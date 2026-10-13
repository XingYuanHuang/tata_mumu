# -*- coding: utf-8 -*-
"""
巡访执行器：拿到UID后在MuMu模拟器中自动执行巡访领礼包流程。
使用MuMu原生SDK(nemu_*)做后台截图+点击，OpenCV模板匹配识别画面状态。
模板配置（阈值+搜索区域）复用塔塔助手exe的template_specs.json。

状态机（复用exe的10个状态）：
  search     → 找朋友页面，点击搜索框，输入UID，回车搜索
  results    → 搜索结果（只有一个匹配），点固定位置头像
  profile    → 玩家资料页，点拜访按钮
  home_gift  → 家园发现礼盒，点礼盒领取
  home_empty → 家园已抢完（没有礼盒图标=空旗），返回找朋友
  home       → 家园图标待确认（中间态，等待识别）
  choose     → 选择祝福弹窗，点选择按钮
  claimed    → 领取结果页，关闭结果页
  honors     → 荣誉墙，没有领取界面，返回找朋友
  unknown    → 未识别
"""
import json
import time
import threading
import subprocess
import cv2
from pathlib import Path


class VisitEngine:
    def __init__(self, mumu_io, assets_dir="visit_assets", on_log=None, delay=0.32):
        self.mumu = mumu_io
        self.assets = Path(assets_dir)
        self.on_log = on_log or (lambda s: None)
        self.width = mumu_io.width
        self.height = mumu_io.height
        self.delay = delay  # 操作间隔秒数（快速0.16/标准0.32/慢速0.65）
        self._specs = {}
        self._templates = {}
        self._load_specs()
        self._load_templates()

    def _load_specs(self):
        path = self.assets / "template_specs.json"
        if path.exists():
            for s in json.loads(path.read_text("utf-8")):
                self._specs[s["name"]] = s
            self.on_log(f"加载模板配置: {len(self._specs)} 项")

    def _load_templates(self):
        for name in self._specs:
            path = self.assets / f"{name}.png"
            if path.exists():
                img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if img is not None:
                    self._templates[name] = img
                    self.on_log(f"  模板 {name}: {img.shape[1]}x{img.shape[0]}")
                else:
                    self.on_log(f"  模板 {name} 读取失败")
            else:
                self.on_log(f"  模板 {name} 文件缺失（用坐标兜底）")

    def screenshot(self):
        return self.mumu.screenshot()

    def _scale_box(self, box):
        """exe坐标(549x1033)→当前分辨率"""
        x1, y1, x2, y2 = box
        sx = self.width / 549.0
        sy = self.height / 1033.0
        return (int(x1*sx), int(y1*sy), int(x2*sx), int(y2*sy))

    def _scale_point(self, x, y):
        sx = self.width / 549.0
        sy = self.height / 1033.0
        return (int(x*sx), int(y*sy))

    def match(self, frame, name, override_threshold=None):
        """用exe的阈值和box匹配，返回(cx,cy,val)或None"""
        spec = self._specs.get(name)
        tpl = self._templates.get(name)
        if spec is None:
            return None

        threshold = override_threshold or spec.get("threshold", 0.88)

        # 没有模板图片时返回None（用坐标兜底）
        if tpl is None:
            return None

        box = self._scale_box(spec["box"])
        x1, y1, x2, y2 = box
        h, w = frame.shape[:2]
        x1 = max(0, min(x1, w-1)); y1 = max(0, min(y1, h-1))
        x2 = max(x1+1, min(x2, w)); y2 = max(y1+1, min(y2, h))
        roi = frame[y1:y2, x1:x2]

        # 缩放模板到ROI比例
        bw = spec["box"][2] - spec["box"][0]
        bh = spec["box"][3] - spec["box"][1]
        rw = x2 - x1; rh = y2 - y1
        scale = min(rw/bw if bw > 0 else 1.0, rh/bh if bh > 0 else 1.0)
        if abs(scale - 1.0) > 0.01:
            tpl_s = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        else:
            tpl_s = tpl
        th2, tw2 = tpl_s.shape[:2]
        if th2 > rh or tw2 > rw:
            return None

        result = cv2.matchTemplate(roi, tpl_s, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= threshold:
            return (x1 + max_loc[0] + tw2//2, y1 + max_loc[1] + th2//2, max_val)
        return None

    def tap(self, x, y, hold_ms=90):
        self.mumu.click(x, y, threading.Event(), lambda p: True, hold_ms=hold_ms)

    def tap_exe(self, ex, ey, hold_ms=90):
        """按exe坐标点击（自动缩放）"""
        cx, cy = self._scale_point(ex, ey)
        self.tap(cx, cy, hold_ms)

    def input_text(self, text):
        subprocess.run(["adb", "-s", self.mumu.serial, "shell", "input", "text", text],
                       capture_output=True, timeout=10)

    def clear_input(self, n=15):
        for _ in range(n):
            subprocess.run(["adb", "-s", self.mumu.serial, "shell", "input", "keyevent", "67"],
                           capture_output=True, timeout=5)

    def key_event(self, keycode=66):
        subprocess.run(["adb", "-s", self.mumu.serial, "shell", "input", "keyevent", str(keycode)],
                       capture_output=True, timeout=10)

    def wait(self, seconds):
        time.sleep(seconds)

    def wait_for_state(self, target_states, timeout=12, interval=0.3):
        """轮询等待直到出现目标状态，返回(state, frame)或(None, None)超时。
        和exe一致：timeout=12秒，interval=0.3秒（exe的delay_range_008_5约0.08~0.5）"""
        if isinstance(target_states, str):
            target_states = {target_states}
        else:
            target_states = set(target_states)
        start = time.time()
        while time.time() - start < timeout:
            frame = self.screenshot()
            state = self.detect_state(frame)
            if state in target_states:
                return state, frame
            time.sleep(interval)
        return None, None

    def tap_and_wait(self, ex, ey, target_states, max_retries=3, timeout=5, action_name="操作"):
        """
        点击exe坐标 + 轮询等待目标状态 + 失败重试。
        和exe的逻辑一致：点击后等画面变化，没变就重试，重试max_retries次后放弃。
        返回(state, frame)成功，或(None, None)失败。
        """
        if isinstance(target_states, str):
            target_states = {target_states}
        else:
            target_states = set(target_states)

        for attempt in range(1, max_retries + 1):
            self.on_log(f"{action_name}（第{attempt}次）")
            self.tap_exe(ex, ey)

            state, frame = self.wait_for_state(target_states, timeout=timeout)
            if state is not None:
                return state, frame

            self.on_log(f"{action_name}未响应，{'重试' if attempt < max_retries else '放弃'}")

        return None, None

    def tap_template_and_wait(self, template_name, target_states, max_retries=3, timeout=5, action_name="操作"):
        """
        用模板匹配定位点击 + 轮询等待 + 失败重试。
        模板匹配不到时用exe box中心兜底。
        """
        if isinstance(target_states, str):
            target_states = {target_states}
        else:
            target_states = set(target_states)

        spec = self._specs.get(template_name)
        if spec is None:
            return None, None

        # exe box中心点
        box = spec["box"]
        ex = (box[0] + box[2]) // 2
        ey = (box[1] + box[3]) // 2

        for attempt in range(1, max_retries + 1):
            # 每次重新截图匹配（画面可能变化）
            frame = self.screenshot()
            pos = self.match(frame, template_name)
            if pos:
                self.on_log(f"{action_name}（第{attempt}次）模板匹配点击")
                self.tap(pos[0], pos[1])
            else:
                self.on_log(f"{action_name}（第{attempt}次）坐标兜底点击")
                self.tap_exe(ex, ey)

            state, frame = self.wait_for_state(target_states, timeout=timeout)
            if state is not None:
                return state, frame

            self.on_log(f"{action_name}未响应，{'重试' if attempt < max_retries else '放弃'}")

        return None, None

    def wait_for_home(self, timeout=10, interval=0.5):
        """等待进入家园页面（home_gift/home_empty/honors），返回(state, frame)或(None,None)"""
        start = time.time()
        while time.time() - start < timeout:
            frame = self.screenshot()
            state = self._is_home_page(frame)
            if state in ("home_gift", "home_empty", "honors"):
                return state, frame
            # 也检查是否到了其他已知状态
            other = self.detect_state(frame)
            if other in ("claimed", "choose"):
                return other, frame
            time.sleep(interval)
        return None, None

    # ── 状态检测（exe的10个状态） ──

    def detect_state(self, frame):
        """
        检测当前画面状态，按exe的优先级。
        search / results / profile / home_gift / home_empty / home / choose / claimed / honors / unknown
        """
        # honors: 荣誉墙（顶部标题）
        if self.match(frame, "honors"):
            return "honors"

        # claimed: 领取结果页（best_title / results_title）
        if self.match(frame, "best_title"):
            return "claimed"
        if self.match(frame, "results_title"):
            return "claimed"

        # choose: 选择祝福
        if self.match(frame, "choice_title"):
            return "choose"

        # home_gift: 发现礼盒（左上角礼盒图标）
        if self.match(frame, "gift"):
            return "home_gift"

        # search: 找朋友页面（搜索图标）
        if self.match(frame, "search_icon"):
            return "search"

        # results: 搜索结果页（"可能认识的玩家"标题）
        if self.match(frame, "suggest_title"):
            return "results"

        return "unknown"

    def _is_home_page(self, frame):
        """在家园页面：有礼盒→home_gift，无礼盒→home_empty
        和exe一致：没有礼盒图标=空旗，但需连续两次确认"""
        if self.match(frame, "gift"):
            return "home_gift"
        if self.match(frame, "honors"):
            return "honors"
        # 第一次没礼盒 → 等 delay 再截一次确认
        self.wait(self.delay)
        frame2 = self.screenshot()
        if self.match(frame2, "gift"):
            return "home_gift"
        if self.match(frame2, "honors"):
            return "honors"
        # 连续两次都没礼盒 → 确认空旗
        return "home_empty"

    # ── 巡访主流程 ──

    def visit_one(self, uid, scan_only=False, timeout=30):
        """
        完整巡访流程，复用exe状态机。
        search → results → profile → home_gift/home_empty → choose → claimed → 返回search
        
        :param scan_only: True=只查找不领取（小号模式），发现可领取礼盒→返回available
                          False=查找并领取（主号模式），完整流程
        """
        self.on_log(f"[{self.mumu.serial}] 开始巡访 UID: {uid} {'（只查找模式）' if scan_only else ''}")

        # 步骤1：确认在找朋友页面（轮询等待，不靠固定wait）
        state, frame = self.wait_for_state("search", timeout=5)
        if state is None:
            self.on_log("不在找朋友页面，尝试返回")
            self._return_to_find_friends()
            state, frame = self.wait_for_state("search", timeout=5)
            if state is None:
                return {"status": "error", "detail": "open_find_friends_first"}

        # 步骤2：点击搜索框，输入UID，回车搜索
        self.on_log("点击搜索框")
        pos = self.match(frame, "search_icon")
        if pos:
            self.tap(pos[0], pos[1])
        else:
            self.tap_exe(532, 533)
        self.wait(self.delay)

        self.on_log(f"输入UID: {uid}")
        self.clear_input()
        self.input_text(uid)
        self.wait(self.delay)

        self.on_log("执行搜索")
        self.key_event(66)  # 回车搜索

        # 和exe一致：搜索后可能键盘未关闭，先等键盘收起
        self.wait(self.delay)
        # 再次按回车确保键盘关闭（exe: search_keyboard_not_closed → demo_keyboard_tap）
        self.key_event(66)
        self.wait(0.3)

        # 步骤3：轮询等待搜索结果（timeout=12，和exe一致）
        # 搜索后可能出现：results(搜索结果)、home_gift/home_empty(直接进家园)、honors(荣誉墙)、claimed(已领过)
        state, frame = self.wait_for_state(
            ("results", "home_gift", "home_empty", "honors", "claimed"),
            timeout=12
        )
        if state is None:
            # 搜索超时，可能结果页出来了但标题没识别到，盲点头像位置
            self.on_log("搜索后未识别到状态，尝试点击头像位置")
            frame = self.screenshot()
            self.tap_exe(523, 853)
            state, frame = self.wait_for_home(timeout=8)
            if state is None:
                self.on_log("点击头像后仍未进入家园")
                self._return_to_find_friends()
                return {"status": "error", "detail": "search_timeout"}

        # 步骤6：根据状态处理
        if state == "claimed":
            self.on_log("已在领取结果页（之前已领取）")
            self._close_result(frame)
            return {"status": "claimed", "detail": "result_page_already_open"}

        if state == "honors":
            self.on_log("荣誉墙，没有领取界面")
            self._return_to_find_friends()
            return {"status": "empty", "detail": "honors_without_gift"}

        if state == "home_empty":
            self.on_log("家园无礼盒（空旗），已抢完")
            self._return_to_find_friends()
            return {"status": "empty", "detail": "empty_flag_confirmed"}

        if state == "results":
            # 步骤4：搜索结果页 → 点头像（重试3次，等进入家园/荣誉墙/结果页）
            state, frame = self.tap_and_wait(
                523, 853, ("home_gift", "home_empty", "honors", "claimed"),
                max_retries=3, timeout=8, action_name="点击头像"
            )
            if state is None:
                # 可能还在资料页，点拜访按钮（重试3次）
                self.on_log("点头像未进家园，尝试拜访按钮")
                state, frame = self.tap_template_and_wait(
                    "visit", ("home_gift", "home_empty", "honors", "claimed"),
                    max_retries=3, timeout=8, action_name="点击拜访"
                )
                if state is None:
                    self.on_log("点击拜访后仍未进入家园")
                    self._return_to_find_friends()
                    return {"status": "error", "detail": "profile_timeout"}

        # 步骤6续：在家园页面处理礼盒
        if state == "home_gift":
            # scan_only模式：发现可领取礼盒，不点击，直接返回
            if scan_only:
                self.on_log("发现可领取礼盒（只查找模式），不领取，返回找朋友")
                self._return_to_find_friends()
                return {"status": "available", "detail": "gift_icon_detected"}

            # 正常模式：点礼盒（重试3次，等选择祝福弹窗或结果页）
            state, frame = self.tap_template_and_wait(
                "gift", ("choose", "claimed"),
                max_retries=3, timeout=8, action_name="点击礼盒"
            )
            if state is None:
                # 点了3次礼盒没响应，检查是否已抢完
                self.on_log("礼盒点击未响应，检查是否已抢完")
                frame = self.screenshot()
                home_state = self._is_home_page(frame)
                if home_state == "home_empty":
                    self.on_log("确认空旗（礼盒已消失）")
                    self._return_to_find_friends()
                    return {"status": "empty", "detail": "gift_no_longer_available"}
                else:
                    self.on_log("礼盒仍在但点击无效，标记需复核")
                    self._return_to_find_friends()
                    return {"status": "error", "detail": "gift_click_not_accepted"}

            if state == "choose":
                self._handle_choice(frame)
                # 选择后等待结果页
                state, frame = self.wait_for_state("claimed", timeout=8)

        elif state == "home_empty":
            self.on_log("家园无礼盒（空旗），已抢完")
            self._return_to_find_friends()
            return {"status": "empty", "detail": "empty_flag_confirmed"}

        elif state == "honors":
            self.on_log("荣誉墙，没有领取界面")
            self._return_to_find_friends()
            return {"status": "empty", "detail": "honors_without_gift"}

        elif state == "claimed":
            pass  # 直接到结果页，下面处理

        else:
            self.on_log(f"家园状态未确认: {state}，等待重试")
            state, frame = self.wait_for_home(timeout=5)
            if state == "home_gift":
                self.on_log("发现礼盒（重试），点击领取")
                pos = self.match(frame, "gift")
                if pos:
                    self.tap(pos[0], pos[1])
                else:
                    self.tap_exe(74, 76)
                state, frame = self.wait_for_state(("choose", "claimed"), timeout=8)
                if state == "choose":
                    self._handle_choice(frame)
                    state, frame = self.wait_for_state("claimed", timeout=8)
            elif state == "home_empty":
                self.on_log("确认空旗")
                self._return_to_find_friends()
                return {"status": "empty", "detail": "empty_flag_confirmed"}
            else:
                self.on_log(f"家园状态仍未确认: {state}")
                self._return_to_find_friends()
                return {"status": "error", "detail": f"home_state={state}"}

        # 步骤7：等待领取结果页
        if state != "claimed":
            state, frame = self.wait_for_state("claimed", timeout=10)

        if state == "claimed":
            self.on_log("领取结果页，领取成功")
            self._close_result(frame)
            return {"status": "claimed", "detail": "gift_icon_detected"}

        # 最后检查：可能礼盒已不可领取
        frame = self.screenshot()
        state = self.detect_state(frame)
        if state == "home_gift":
            self.on_log("礼盒仍在，可能已不可领取")
            self._return_to_find_friends()
            return {"status": "empty", "detail": "gift_no_longer_available"}
        if state == "home_empty":
            self.on_log("确认空旗")
            self._return_to_find_friends()
            return {"status": "empty", "detail": "empty_flag_confirmed"}

        self.on_log(f"巡访结束，最终状态: {state}")
        self._return_to_find_friends()
        return {"status": "error", "detail": f"final_state={state}"}

    def _handle_choice(self, frame):
        """选择祝福弹窗 → 点选择按钮"""
        self.on_log("选择祝福弹窗，点击选择按钮")
        pos = self.match(frame, "choose_button")
        if pos:
            self.tap(pos[0], pos[1])
        else:
            self.tap_exe(164, 918)  # choose_button box中心

    def _close_result(self, frame):
        """关闭结果页"""
        self.on_log("关闭结果页")
        pos = self.match(frame, "close")
        if pos:
            self.tap(pos[0], pos[1])
        else:
            pos = self.match(frame, "ok")
            if pos:
                self.tap(pos[0], pos[1])
            else:
                self.tap_exe(292, 1000)  # close box中心
        self.wait(1)

    def _return_to_find_friends(self):
        """返回找朋友页面"""
        self.on_log("返回找朋友页面")
        frame = self.screenshot()
        pos = self.match(frame, "close")
        if pos:
            self.tap(pos[0], pos[1])
            self.wait(1)
            return
        pos = self.match(frame, "ok")
        if pos:
            self.tap(pos[0], pos[1])
            self.wait(1)
            return
        self.key_event(4)  # 返回键
        self.wait(1)
