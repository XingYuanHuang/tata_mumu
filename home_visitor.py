# -*- coding: utf-8 -*-
"""「进家园」轻量动作：面板「只进家园」按钮触发，进对方家园并领礼盒，不进任务队列。

和主号通道「完整领取」的区别：
  - 同样走 搜索 UID → 拜访 → 进家园 → 领礼盒 → 选祝福，但领完**停在家园**，
    由用户手动退出（下次触发会自动先返回找朋友页再搜下一个）
  - 不进主号队列：独立单线程 worker 串行执行
  - 有自己的记录（落盘 home_visited.json，前端面板展示）：进成功过的 UID 再提交也不进
    （错误不记，允许重试）
  - 和队列任务共用引擎：通过 engine_gate 每实例互斥锁排队，不会同时操作同一模拟器
"""
import json
import os
import re
import threading
import time
from collections import deque

from config import UID_LENGTH
from engine_gate import lock_for

MAX_RECORDS = 500          # 落盘保留条数
# visit_one(scan_only=True) 可能返回的"算成功"状态（都表示真的进过家园/确认过结果）
OK_STATUSES = ("available", "empty", "claimed")

STATUS_LABEL = {
    "claimed": "领到礼盒",
    "available": "进家园·有礼盒（未领）",
    "empty": "进家园·空旗（已抢完）",
    "not_found": "UID 无效",
    "error": "失败",
}


class HomeVisitor:
    """单线程串行的进家园执行器。

    :param get_engines: 返回 {name: VisitEngine} 的可调用（引用 server 的字典）
    :param is_main: is_main(name) -> bool，判断是否主号实例
    :param on_record: 每产生一条新记录时回调 record（用于 WebSocket 推前端）
    """

    def __init__(self, get_engines, is_main, on_log=None, on_record=None,
                 state_path="home_visited.json"):
        self.get_engines = get_engines
        self.is_main = is_main
        self.on_log = on_log or (lambda s: None)
        self.on_record = on_record
        self.state_path = state_path

        self._records = []        # 新的在前: {uid,time,instances:{name:status},status}
        self._ok_uids = set()     # 成功进过家园的 UID（去重用）
        self._lock = threading.RLock()
        self._queue = deque()
        self._wake = threading.Event()
        self._running = False
        self._thread = None
        self.triggered = 0        # 累计触发
        self.skipped = 0          # 累计跳过（重复）
        self._load()

    # ── 记录落盘 ──

    def _load(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            return
        recs = data.get("records") or []
        for r in recs[:MAX_RECORDS]:
            if isinstance(r, dict) and r.get("uid"):
                self._records.append(r)
                if r.get("status") in OK_STATUSES:
                    self._ok_uids.add(str(r["uid"]))

    def _save(self):
        with self._lock:
            data = {"records": self._records[:MAX_RECORDS]}
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception as e:
            self.on_log(f"进家园记录保存失败（不影响执行）: {e}")

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="home-visitor")
        self._thread.start()
        n = len(self._ok_uids)
        self.on_log(f"进家园执行器已启动（独立通道，不进任务队列，历史记录 {n} 个 UID）")

    def stop(self):
        self._running = False
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        self.on_log("进家园执行器已停止")

    # ── 提交入口（剪贴板回调 / 面板按钮） ──

    def submit(self, uid, instance=None):
        """请求进一个家园。

        返回 "queued"（已受理）/ "skipped"（进过，别再来）/ "error"（没跑起来，可重试）。
        """
        uid = re.sub(r"\D", "", str(uid or ""))
        if len(uid) != UID_LENGTH:
            self.on_log(f"进家园被拒绝: UID 必须是 {UID_LENGTH} 位数字")
            return "error"

        with self._lock:
            if uid in self._ok_uids:
                self.skipped += 1
                self.on_log(f"UID {uid} 之前已进过家园，跳过（清空进家园记录可重进）")
                return "skipped"

        engines = self.get_engines()
        if instance:
            names = [instance] if instance in engines else []
        else:
            names = [n for n in engines if self.is_main(n)]
        if not names:
            self.on_log(f"进家园 UID {uid} 失败: 没有可用的巡访引擎"
                        f"（{'实例不存在或未连接' if instance else '主号引擎未初始化'}）")
            return "error"

        self._queue.append((uid, tuple(names)))
        self._wake.set()
        self.triggered += 1
        self.on_log(f"进家园请求已受理: UID {uid} → {'、'.join(names)}（队列 {len(self._queue)} 个）")
        return "queued"

    # ── worker ──

    def _worker(self):
        while self._running:
            item = None
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
            if item is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self._enter_one(*item)
            except Exception as e:
                self.on_log(f"进家园执行异常: {e}")

    def _enter_one(self, uid, names):
        """在一个或多个实例上进家园（并行线程，和主号队列一样）。"""
        results = {}

        def run(name):
            try:
                engine = self.get_engines().get(name)
                if engine is None:
                    results[name] = {"status": "error", "detail": "无巡访引擎"}
                    return
                self.on_log(f"[{name}] 进家园 UID: {uid}（领礼盒，领完停在家园）")
                with lock_for(name):
                    result = engine.visit_one(uid, scan_only=False, stay=True)
                results[name] = result
                self.on_log(f"[{name}] 进家园结果: {result}")
            except Exception as e:
                results[name] = {"status": "error", "detail": str(e)}
                self.on_log(f"[{name}] 进家园异常: {e}")

        threads = [threading.Thread(target=run, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 汇总：任一实例真进去了就算成功；全 not_found 记无效；否则 error（可重试）
        statuses = {n: r.get("status", "error") for n, r in results.items()}
        if any(s in OK_STATUSES for s in statuses.values()):
            summary = next(s for s in ("claimed", "available", "empty")
                           if s in statuses.values())
        elif all(s == "not_found" for s in statuses.values()):
            summary = "not_found"
        else:
            summary = "error"

        rec = {
            "uid": uid,
            "time": time.strftime("%m-%d %H:%M:%S"),
            "instances": statuses,
            "status": summary,
            "label": STATUS_LABEL.get(summary, summary),
        }
        with self._lock:
            self._records.insert(0, rec)
            del self._records[MAX_RECORDS:]
            if summary in OK_STATUSES:
                self._ok_uids.add(uid)
        self._save()
        if self.on_record:
            try:
                self.on_record(rec)
            except Exception:
                pass

    # ── 查询 / 清理 ──

    def records(self, limit=200):
        with self._lock:
            return list(self._records[:limit])

    def clear(self):
        with self._lock:
            n = len(self._records)
            self._records.clear()
            self._ok_uids.clear()
        self._save()
        return n

    def status(self):
        with self._lock:
            return {
                "records": list(self._records[:100]),
                "total": len(self._records),
                "ok_uids": len(self._ok_uids),
                "pending": len(self._queue),
                "triggered": self.triggered,
                "skipped": self.skipped,
            }
