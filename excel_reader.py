# -*- coding: utf-8 -*-
"""
Excel UID 循环读取器。
后台线程反复读取 Excel 中的 UID 列表，逐个提交给小号通道处理。
操作成功后在 Excel 中标记，避免重复处理。
"""
import os
import threading
import time

from openpyxl import load_workbook


class ExcelReader:
    def __init__(self, config, task_handler, on_log=None):
        """
        :param config: EXCEL_CONFIG 字典
        :param task_handler: TaskHandler 实例
        :param on_log: 日志回调 callback(log_str)
        """
        self.file_path = config.get("file_path", "uid_list.xlsx")
        self.uid_column = config.get("uid_column", "uid")
        self.poll_interval = config.get("poll_interval", 5.0)
        self.mark_processed = config.get("mark_processed", True)
        self.processed_column = config.get("processed_column", "processed")

        self.task_handler = task_handler
        self.on_log = on_log or (lambda s: None)
        self._running = False
        self._thread = None
        self._current_uid = None  # 当前正在查找的UID
        self._round = 0           # 当前循环轮次

        # 记录已提交的UID，防止同一轮重复提交
        self._submitted_uids = set()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.on_log("Excel读取线程已启动")

    def stop(self):
        self._running = False
        self.on_log("Excel读取线程已停止")

    def get_status(self):
        """返回Excel UID列表和查找状态，供前端展示"""
        result = {
            "running": self._running,
            "file_path": self.file_path,
            "current_uid": self._current_uid,
            "round": self._round,
            "total": 0, "processed": 0, "pending": 0,
            "uids": [],
        }
        try:
            if not os.path.isfile(self.file_path):
                result["error"] = "文件不存在"
                return result

            wb = load_workbook(self.file_path)
            ws = wb.active

            headers = {}
            for col in range(1, ws.max_column + 1):
                val = ws.cell(row=1, column=col).value
                if val:
                    headers[str(val).strip()] = col

            if self.uid_column not in headers:
                result["error"] = f"未找到列: {self.uid_column}"
                wb.close()
                return result

            uid_col = headers[self.uid_column]
            processed_col = headers.get(self.processed_column)

            for row in range(2, ws.max_row + 1):
                uid = ws.cell(row=row, column=uid_col).value
                if uid is None:
                    continue
                uid_str = str(uid).strip()
                marked = ""
                if processed_col:
                    v = ws.cell(row=row, column=processed_col).value
                    marked = str(v).strip() if v else ""

                status = "pending"
                if marked in ("1", "available", "done"):
                    status = "processed"
                # 检查任务结果
                for t in self.task_handler.get_all_tasks():
                    if t.digit_id == uid_str and t.source == "Excel":
                        if t.status == "done":
                            r = t.visit_result or {}
                            detail = list(r.values())[0] if r else {}
                            if detail.get("status") == "available":
                                status = "available"
                            elif detail.get("status") == "empty":
                                status = "empty"
                            elif detail.get("status") == "claimed":
                                status = "claimed"
                        elif t.status == "error":
                            status = "error"

                result["uids"].append({
                    "row": row,
                    "uid": uid_str,
                    "status": status,
                    "is_current": uid_str == self._current_uid,
                })

                result["total"] += 1
                if status in ("available", "empty", "claimed", "processed"):
                    result["processed"] += 1
                else:
                    result["pending"] += 1

            wb.close()
        except Exception as e:
            result["error"] = str(e)

        return result

    def _loop(self):
        while self._running:
            try:
                self._read_once()
            except FileNotFoundError:
                self.on_log(f"Excel文件不存在: {self.file_path}")
            except Exception as e:
                self.on_log(f"Excel读取错误: {e}")

            # 等待下一轮
            for _ in range(int(self.poll_interval)):
                if not self._running:
                    break
                time.sleep(1)

    def _read_once(self):
        """读取一次Excel，把未处理的UID提交到小号通道"""
        if not os.path.isfile(self.file_path):
            raise FileNotFoundError(self.file_path)

        self._round += 1

        wb = load_workbook(self.file_path)
        ws = wb.active

        # 找到列索引
        headers = {}
        for col in range(1, ws.max_column + 1):
            val = ws.cell(row=1, column=col).value
            if val:
                headers[str(val).strip()] = col

        if self.uid_column not in headers:
            self.on_log(f"Excel中未找到列: {self.uid_column}")
            wb.close()
            return

        uid_col = headers[self.uid_column]
        processed_col = headers.get(self.processed_column)

        # 如果需要标记但列不存在，自动创建
        if self.mark_processed and processed_col is None:
            processed_col = ws.max_column + 1
            ws.cell(row=1, column=processed_col, value=self.processed_column)

        count = 0
        for row in range(2, ws.max_row + 1):
            uid = ws.cell(row=row, column=uid_col).value
            if uid is None:
                continue

            uid_str = str(uid).strip()

            # 检查是否已标记处理（标记值=1表示已发现可领取，跳过）
            if self.mark_processed and processed_col:
                marked = ws.cell(row=row, column=processed_col).value
                if marked and str(marked).strip() in ("1", "available", "done"):
                    continue

            # 提交到小号通道
            self._current_uid = uid_str
            task = self.task_handler.submit_from_excel(uid_str)
            if task:
                count += 1
                self.on_log(f"Excel提交UID: {uid_str} (行{row})")

                # 等待这个任务完成再处理下一个（小号是串行的）
                self._wait_task_done(task)

                # 根据查找结果标记
                if self.mark_processed and processed_col:
                    result_status = task.visit_result.get(list(task.visit_result.keys())[0], {}).get("status", "") if task.visit_result else ""
                    if task.status == "done" and result_status == "available":
                        # 发现可领取 → 标记为1，下次跳过
                        ws.cell(row=row, column=processed_col, value="1")
                        self.on_log(f"UID {uid_str} 标记为可领取，下次跳过")
                    elif task.status == "done" and result_status in ("empty", "claimed"):
                        # 已抢完或已领取 → 也标记跳过
                        ws.cell(row=row, column=processed_col, value="1")
                        self.on_log(f"UID {uid_str} {result_status}，标记跳过")
                    else:
                        # 出错 → 不标记，下次循环重新检查
                        self.on_log(f"UID {uid_str} 查找失败，不标记，下次重试")

        # 保存Excel（更新标记）
        if self.mark_processed and count > 0:
            try:
                wb.save(self.file_path)
            except PermissionError:
                self.on_log("Excel保存失败：文件被占用，请关闭Excel后重试")

        wb.close()
        self._current_uid = None

        if count == 0:
            self.on_log("Excel本轮无新UID需要处理")
        else:
            self.on_log(f"Excel本轮提交了{count}个UID")

    def _wait_task_done(self, task, timeout=120):
        """等待任务完成"""
        start = time.time()
        while time.time() - start < timeout:
            if task.status in ("done", "error"):
                return
            if not self._running:
                return
            time.sleep(0.5)
