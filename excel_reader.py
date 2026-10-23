# -*- coding: utf-8 -*-
"""
Excel UID 循环读取器（**不依赖表头/列名**）。

读取规则（用户确认）：
- 扫描表里**所有单元格**，提取其中所有 N 位数字 UID（N = config.UID_LENGTH，默认8）
- 按出现顺序**去重**（同一个 UID 只处理一次）
- 逐个提交给小号通道处理（小号串行），处理结果可在右侧「processed」列打标记，
  仅用于跨重启跳过，不参与 UID 识别
"""
import os
import re
import threading
import time

from openpyxl import load_workbook

import task_handler as th


def _uid_len():
    return th.UID_LENGTH


def _uid_re():
    return re.compile(r"(?<!\d)(\d{%d})(?!\d)" % _uid_len())


def is_uid_str(s):
    """纯数字且长度等于 UID 位数"""
    return isinstance(s, str) and s.isdigit() and len(s) == _uid_len()


def extract_uids(value):
    """从一个单元格的值里提取所有 UID（返回列表，可能为空）。

    兼容：
    - 数字单元格（int / float，如 12345678 或 1.2345678E7）
    - 文本单元格（"12345678"、带空格、或夹在文字里如 "UID:12345678"）
    - 其它类型（日期、布尔、公式报错等）→ 忽略
    """
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, int):
        s = str(value)
        return [s] if is_uid_str(s) else []
    if isinstance(value, float):
        # Excel 数字可能是 12345678.0 / 1.2345678E7
        if value != value or value in (float("inf"), float("-inf")):
            return []
        s = str(int(value)) if float(value).is_integer() else str(value)
        return [s] if is_uid_str(s) else []
    if not isinstance(value, str):
        return []          # datetime 等其它类型直接忽略

    s = value.strip()
    if not s:
        return []
    if s.isdigit():
        return [s] if is_uid_str(s) else []
    # 文本里夹着 UID → 提取独立（左右不是数字）的 N 位数字
    return _uid_re().findall(s)


class ExcelReader:
    def __init__(self, config, task_handler, on_log=None):
        """
        :param config: EXCEL_CONFIG 字典
        :param task_handler: TaskHandler 实例
        :param on_log: 日志回调 callback(log_str)
        """
        self.file_path = config.get("file_path", "uid_list.xlsx")
        self.poll_interval = config.get("poll_interval", 5.0)
        # 处理完成后在表右侧写标记（跨重启跳过用）；不做 UID 识别
        self.mark_processed = config.get("mark_processed", True)
        self.processed_column = config.get("processed_column", "processed")

        self.task_handler = task_handler
        self.on_log = on_log or (lambda s: None)
        self._running = False
        self._thread = None
        self._current_uid = None  # 当前正在查找的UID
        self._round = 0           # 当前循环轮次
        # 多遍重扫：rescan(N) 后自动跑完 N 遍（每遍结束自动清标记重来）
        self._auto_passes = 0     # 本遍结束后还要再扫的遍数
        self._pass_no = 0         # 当前是第几遍
        self._pass_total = 0      # 总共要扫几遍
        self._pass_submitted = 0  # 本遍已提交的任务数（判断本遍是否真的跑过）

    # ── 对外接口 ──

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

    # ── 多遍重扫 ──

    def rescan(self, passes=2):
        """清掉所有已处理标记，然后把整张名单连续重扫 passes 遍。

        每跑完一遍会自动再清一次标记（文件标记 + 内存已处理集合），
        接着跑下一遍；全部跑完自动停在轮询状态。
        """
        if not os.path.isfile(self.file_path):
            self.on_log(f"重扫失败：Excel 文件不存在 {self.file_path}")
            return False
        passes = max(1, int(passes))
        if not self.reset_marks():
            return False
        self._pass_no = 1
        self._pass_total = passes
        self._auto_passes = passes - 1
        self._pass_submitted = 0
        self.on_log(f"开始第 1/{passes} 遍重扫（共 {passes} 遍）")
        if not self._running:
            self.start()
        return True

    def reset_marks(self):
        """清除表内已处理标记 + 内存已处理集合，让名单可以重新巡访"""
        cleared = 0
        try:
            wb = load_workbook(self.file_path)
            ws = wb.active
            # 优先只清「processed」标记列
            mark_col = None
            for r in range(1, min(6, ws.max_row) + 1):
                for c in range(1, ws.max_column + 1):
                    v = ws.cell(row=r, column=c).value
                    if v is not None and str(v).strip() == self.processed_column:
                        mark_col = c
                        break
                if mark_col:
                    break

            if mark_col:
                rng = ((r, mark_col) for r in range(1, ws.max_row + 1))
            else:
                # 历史上标记是直接写在 UID 列右侧若干列的「1」→ 一并清掉
                rng = ((r, c) for r in range(1, ws.max_row + 1)
                       for c in range(2, ws.max_column + 1))

            for r, c in rng:
                cell = ws.cell(row=r, column=c)
                if cell.value is not None and str(cell.value).strip() in ("1", "available", "done"):
                    cell.value = None
                    cleared += 1
            wb.save(self.file_path)
            wb.close()
        except PermissionError:
            self.on_log("重扫失败：Excel 文件被占用，请先关闭表格再试")
            return False
        except Exception as e:
            self.on_log(f"重扫清标记失败: {e}")
            return False

        n = self.task_handler.clear_processed()
        self.on_log(f"已清除标记 {cleared} 个、内存已处理 {n} 个，名单将重新巡访")
        return True

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

            wb = load_workbook(self.file_path, data_only=True, read_only=True)
            ws = wb.active
            found = self._scan_sheet(ws)
            try:
                wb.close()
            except Exception:
                pass

            # 每个 UID 的查找结果（来自任务记录）
            task_status = {}
            for t in self.task_handler.get_all_tasks():
                if t.source == "Excel":
                    r = t.visit_result or {}
                    detail = next(iter(r.values()), {}) if r else {}
                    st = detail.get("status")
                    if t.status == "done" and st in ("available", "empty", "claimed"):
                        task_status[t.digit_id] = st
                    elif t.status == "error" and t.digit_id not in task_status:
                        task_status[t.digit_id] = "error"
                    elif t.status == "running":
                        task_status[t.digit_id] = "running"

            for idx, item in enumerate(found):
                uid = item["uid"]
                status = task_status.get(uid, "pending")
                if status == "pending" and self.task_handler._is_processed(uid):
                    status = "processed"
                result["uids"].append({
                    "index": idx + 1,
                    "row": item["row"],
                    "uid": uid,
                    "status": status,
                    "is_current": uid == self._current_uid,
                })
                result["total"] += 1
                if status in ("available", "empty", "claimed", "processed"):
                    result["processed"] += 1
                else:
                    result["pending"] += 1
        except Exception as e:
            result["error"] = str(e)

        return result

    # ── 内部实现 ──

    def _scan_sheet(self, ws):
        """扫描整张表所有单元格，按出现顺序提取去重后的 UID。

        返回 [{"uid": "12345678", "row": 2, "col": 1}, ...]
        """
        seen = set()
        found = []
        for row in ws.iter_rows():
            for cell in row:
                for uid in extract_uids(cell.value):
                    if uid in seen:
                        continue
                    seen.add(uid)
                    found.append({"uid": uid, "row": cell.row, "col": cell.column})
        return found

    def _header_row(self, ws, scan=6):
        """猜表头所在行：前几行里非空单元格最多的那一行。

        你这个表第 1 行是群名、第 2 行才是表头，所以不能写死在 row 1。
        """
        best, best_cnt = 1, -1
        for r in range(1, min(scan, ws.max_row) + 1):
            cnt = sum(1 for c in ws[r] if c.value is not None)
            if cnt > best_cnt:
                best, best_cnt = r, cnt
        return best

    def _find_processed_col(self, ws):
        """找（或创建）标记列。只用于写标记，不参与 UID 识别。"""
        # 先在前几行里找已有的同名表头
        for r in range(1, min(6, ws.max_row) + 1):
            for col in range(1, ws.max_column + 1):
                val = ws.cell(row=r, column=col).value
                if val and str(val).strip() == self.processed_column:
                    return col
        # 没有就在最右侧新建一列，表头写在真正的表头行上
        col = ws.max_column + 1
        ws.cell(row=self._header_row(ws), column=col, value=self.processed_column)
        return col

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
        """读取一次Excel，把未处理的 UID 提交到小号通道"""
        if not os.path.isfile(self.file_path):
            raise FileNotFoundError(self.file_path)

        self._round += 1

        wb = load_workbook(self.file_path)
        ws = wb.active

        found = self._scan_sheet(ws)
        if not found:
            self.on_log("Excel里没有找到任何 UID（扫描全部单元格）")
            wb.close()
            return

        processed_col = None
        if self.mark_processed:
            processed_col = self._find_processed_col(ws)

        count = 0
        for item in found:
            uid_str, row = item["uid"], item["row"]

            # 已打标记 → 跳过（跨重启避免重复）
            if processed_col:
                marked = ws.cell(row=row, column=processed_col).value
                if marked and str(marked).strip() in ("1", "available", "done"):
                    continue

            self._current_uid = uid_str
            task = self.task_handler.submit_from_excel(uid_str)
            if not task:
                # 已在「已处理」集合里 → 补个标记，以后不再考虑
                if processed_col:
                    ws.cell(row=row, column=processed_col, value="1")
                continue

            count += 1
            self.on_log(f"Excel提交UID: {uid_str}（第{item['row']}行第{item['col']}列）")

            # 等待这个任务完成再处理下一个（小号是串行的）
            self._wait_task_done(task)

            if processed_col:
                r = task.visit_result or {}
                detail = next(iter(r.values()), {}) if r else {}
                result_status = detail.get("status", "")
                if task.status == "done" and result_status in ("available", "empty", "claimed"):
                    ws.cell(row=row, column=processed_col, value="1")
                    self.on_log(f"UID {uid_str} {result_status}，标记跳过")
                elif task.status == "done":
                    # UID 不存在等（not_found 已在 handler 里记入已处理）
                    ws.cell(row=row, column=processed_col, value="1")
                else:
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
            # 本遍扫完（整张名单都打上标记了）→ 若还要再扫几遍，自动清标记续扫
            need_next = self._auto_passes > 0 and (self._pass_submitted > 0 or self._pass_no > 0)
            if need_next:
                self._auto_passes -= 1
                self._pass_no += 1
                self._pass_submitted = 0
                if self.reset_marks():
                    self.on_log(f"开始第 {self._pass_no}/{self._pass_total} 遍重扫")
                    return
                self.on_log("下一遍启动失败，停止重扫")
                self._auto_passes = 0
                self._pass_total = 0
                self._pass_no = 0
            else:
                if self._pass_total and self._pass_no >= self._pass_total:
                    self.on_log(f"{self._pass_total} 遍重扫全部完成")
                    self._pass_total = 0
                    self._pass_no = 0
                self.on_log("Excel本轮无新UID需要处理")
        else:
            self._pass_submitted += count
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
