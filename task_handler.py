# -*- coding: utf-8 -*-
"""
任务处理器：多通道单线程队列。
- 主号线程：收到UID → 主号实例执行巡访流程
- 小号线程：循环读Excel UID → 操作成功后同步给主号线程
- 每个通道内部串行，通道间并行
"""
import re
import time
import threading
import queue

from adb_controller import ADBManager


# UID位数，可通过config.py配置
UID_LENGTH = 8

_uid_pattern = None
def _get_pattern():
    global _uid_pattern
    if _uid_pattern is None:
        _uid_pattern = re.compile(r"^\d{%d}$" % UID_LENGTH)
    return _uid_pattern

def set_uid_length(n):
    global UID_LENGTH, _uid_pattern
    UID_LENGTH = n
    _uid_pattern = None

def is_uid(text):
    """判断文本是否为纯数字UID（位数由UID_LENGTH决定）"""
    text = text.strip()
    return bool(_get_pattern().match(text))

# 兼容旧调用
is_12_digit = is_uid


class TaskItem:
    def __init__(self, task_id, digit_id, source, message, channel="main"):
        self.task_id = task_id
        self.digit_id = digit_id
        self.source = source
        self.message = message
        self.channel = channel
        self.status = "pending"
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None
        self.error = None
        self.executed_on = []
        self.visit_result = None  # 巡访结果详情


class TaskHandler:
    def __init__(self, adb_config, excel_config=None, on_status_change=None,
                 on_log=None, visit_engines=None):
        self.adb_manager = ADBManager(
            adb_config.get("instances", {}),
            adb_path=adb_config.get("adb_path"),
        )
        self.excel_config = excel_config or {}
        self.on_status_change = on_status_change or (lambda task: None)
        self.on_log = on_log or (lambda s: None)

        # 巡访引擎字典: {instance_name: VisitEngine}
        # 由外部传入，因为VisitEngine需要MuMuIO实例
        self.visit_engines = visit_engines or {}

        self._main_queue = queue.Queue()
        self._worker_queue = queue.Queue()
        self._tasks = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._running = False
        self._main_thread = None
        self._worker_thread = None

    def _gen_id(self):
        with self._lock:
            self._counter += 1
            return f"task_{self._counter}"

    def set_visit_engines(self, engines):
        """动态设置巡访引擎"""
        self.visit_engines = engines

    def start(self):
        if self._running:
            return
        self._running = True
        self._main_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._main_thread.start()
        self._worker_thread.start()

    def stop(self):
        self._running = False
        self._main_queue.put(None)
        self._worker_queue.put(None)

    # ── 主号线程：实例1 + 实例2 同时执行巡访 ──

    def _main_loop(self):
        while self._running:
            task = self._main_queue.get()
            if task is None:
                break
            self._execute_main(task)

    def _execute_main(self, task):
        task.status = "running"
        self.on_status_change(task)

        main_instances = self.adb_manager.get_main_instances()

        if not main_instances:
            task.status = "error"
            task.error = "没有主号实例（请在面板上将至少一个实例角色设为主号）"
            task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.on_status_change(task)
            return

        # 过滤出有巡访引擎且ADB已连接的主号实例
        runnable = {}
        for name, inst in main_instances.items():
            if name not in self.visit_engines:
                self.on_log(f"[{name}] 无巡访引擎，跳过")
                continue
            if not inst.ensure_connected():
                self.on_log(f"[{name}] ADB连接失败，跳过")
                continue
            runnable[name] = inst

        if not runnable:
            task.status = "error"
            task.error = "没有可用的主号实例（巡访引擎未初始化或ADB未连接）"
            task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.on_status_change(task)
            return

        # 每个主号实例各起一个线程，同时执行巡访
        threads = []
        results = {}

        def run_visit(name, inst, task):
            try:
                engine = self.visit_engines.get(name)
                if engine is None:
                    results[name] = {"status": "error", "detail": "无巡访引擎"}
                    return
                self.on_log(f"[{name}] 开始巡访 UID: {task.digit_id}")
                result = engine.visit_one(task.digit_id)
                results[name] = result
                task.executed_on.append(name)
                self.on_log(f"[{name}] 巡访结果: {result}")
            except Exception as e:
                results[name] = {"status": "error", "detail": str(e)}
                self.on_log(f"[{name}] 巡访异常: {e}")

        for name, inst in runnable.items():
            t = threading.Thread(target=run_visit, args=(name, inst, task))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # 汇总结果
        all_ok = all(r.get("status") in ("claimed", "empty") for r in results.values()) if results else True
        task.visit_result = results
        task.status = "done" if all_ok else "error"
        if not all_ok:
            task.error = "; ".join(f"{k}: {v.get('detail','')}" for k, v in results.items() if v.get("status") == "error")
        task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if task.status == "done":
            self._mark_processed(task.digit_id)
        self.on_status_change(task)

    # ── 小号线程：独立执行，成功后同步给主号 ──

    def _worker_loop(self):
        while self._running:
            task = self._worker_queue.get()
            if task is None:
                break
            self._execute_worker(task)

    def _execute_worker(self, task):
        task.status = "running"
        self.on_status_change(task)

        worker_instances = self.adb_manager.get_worker_instances()

        for name, inst in worker_instances.items():
            if not inst.ensure_connected():
                task.status = "error"
                task.error = f"{name} ADB连接失败"
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.on_status_change(task)
                return

            try:
                engine = self.visit_engines.get(name)
                if engine is None:
                    task.status = "error"
                    task.error = f"{name} 无巡访引擎"
                    task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.on_status_change(task)
                    return

                self.on_log(f"[{name}] 小号查找 UID: {task.digit_id}（只查找不领取）")
                result = engine.visit_one(task.digit_id, scan_only=True)
                task.executed_on.append(name)
                task.visit_result = {name: result}
                self.on_log(f"[{name}] 小号查找结果: {result}")

                if result.get("status") == "available":
                    # 发现可领取礼盒 → 同步给主号去领取
                    task.status = "done"
                    task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._mark_processed(task.digit_id)
                    self.on_status_change(task)
                    self._sync_to_main(task)
                elif result.get("status") == "empty":
                    # 已抢完/空旗 → 标记完成，不同步给主号
                    task.status = "done"
                    task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._mark_processed(task.digit_id)
                    self.on_status_change(task)
                elif result.get("status") == "claimed":
                    # 已领取过 → 标记完成，不同步
                    task.status = "done"
                    task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._mark_processed(task.digit_id)
                    self.on_status_change(task)
                else:
                    # 出错 → 标记失败，不标记已处理（下次可重试）
                    task.status = "error"
                    task.error = result.get("detail", "查找失败")
                    task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.on_status_change(task)

            except Exception as e:
                task.status = "error"
                task.error = str(e)
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.on_status_change(task)

    def _sync_to_main(self, worker_task):
        """小号发现可领取礼盒后，把UID同步给主号去领取"""
        uid = worker_task.digit_id
        if self._is_processed(uid):
            self.on_log(f"小号同步UID {uid} 已被主号处理过，跳过")
            return
        # 没有主号时跳过同步，不阻塞小号
        if not self.adb_manager.get_main_instances():
            self.on_log(f"小号发现UID {uid} 可领取，但没有主号在线，仅记录不同步")
            return
        sync_task_id = self._gen_id()
        sync_task = TaskItem(
            task_id=sync_task_id,
            digit_id=uid,
            source=f"同步(来自小号)",
            message={"content": uid, "platform": "同步"},
            channel="main",
        )
        with self._lock:
            self._tasks[sync_task_id] = sync_task
        self._main_queue.put(sync_task)
        self.on_status_change(sync_task)
        self.on_log(f"小号同步UID {uid} 给主号线程")

    # ── 提交任务 ──

    # 已处理过的UID集合，避免重复巡访
    _processed_uids = set()

    def _is_processed(self, uid):
        with self._lock:
            return uid in self._processed_uids

    def _mark_processed(self, uid):
        with self._lock:
            self._processed_uids.add(uid)

    def submit_from_message(self, message):
        content = message.get("content", "").strip()
        if not is_12_digit(content):
            return None
        if self._is_processed(content):
            self.on_log(f"UID {content} 已处理过，跳过")
            return None
        task_id = self._gen_id()
        task = TaskItem(task_id, content, message.get("platform", "?"), message, "main")
        with self._lock:
            self._tasks[task_id] = task
        self._main_queue.put(task)
        self.on_status_change(task)
        return task

    def submit_from_excel(self, uid):
        uid = str(uid).strip()
        if not is_12_digit(uid):
            return None
        if self._is_processed(uid):
            self.on_log(f"UID {uid} 已处理过，跳过")
            return None
        task_id = self._gen_id()
        task = TaskItem(task_id, uid, "Excel", {"content": uid, "platform": "Excel"}, "worker")
        with self._lock:
            self._tasks[task_id] = task
        self._worker_queue.put(task)
        self.on_status_change(task)
        return task

    # ── 查询 ──

    def get_all_tasks(self):
        with self._lock:
            return list(self._tasks.values())

    def get_stats(self):
        with self._lock:
            tasks = list(self._tasks.values())
        return {
            "total": len(tasks),
            "pending": sum(1 for t in tasks if t.status == "pending"),
            "running": sum(1 for t in tasks if t.status == "running"),
            "done": sum(1 for t in tasks if t.status == "done"),
            "error": sum(1 for t in tasks if t.status == "error"),
        }

    def task_to_dict(self, task):
        return {
            "task_id": task.task_id,
            "digit_id": task.digit_id,
            "source": task.source,
            "channel": task.channel,
            "status": task.status,
            "created_at": task.created_at,
            "finished_at": task.finished_at,
            "error": task.error,
            "executed_on": task.executed_on,
            "visit_result": task.visit_result,
            "group": task.message.get("group", ""),
            "sender": task.message.get("sender", ""),
        }
