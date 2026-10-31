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
    def __init__(self, mumu_io, assets_dir="visit_assets", on_log=None, delay=0.32,
                 adb_path=None):
        self.mumu = mumu_io
        self.assets = Path(assets_dir)
        self.on_log = on_log or (lambda s: None)
        self.width = mumu_io.width
        self.height = mumu_io.height
        self.delay = delay  # 操作间隔秒数（快速0.16/标准0.32/慢速0.65）
        # 进入对方家园后的**最长**等待秒数：镜头约 2~3 秒位移到礼盒位置。
        # 实际不停等满：轮询期间一旦看到礼盒/空旗气泡就立刻判定返回（见 wait_for_home）。
        self.home_dwell = 3.0
        # 主号领取模式：进家园瞬间先手点击礼盒位置（见 GIFT_CENTER_EXE）；
        # 小号查找模式必须关掉（不能替主号把面板点开）。visit_one 里按 scan_only 设置。
        self.pre_tap_gift = False
        self._pre_tapped = False
        # adb 可执行文件路径；None = 首次用时再解析（见 _resolve_adb）
        self.adb_path = adb_path
        self._specs = {}
        self._templates = {}
        self._load_specs()
        self._load_templates()

    # ── adb 输入通道 ──
    # MuMu 原生 SDK(nemu_*) 只提供触摸能力，没有文字输入，
    # 所以「输入 UID」这一步必须借道 adb。
    # 本机没有把 adb 装进 PATH（config.ADB_PATH = None），
    # 直接调用裸命令 "adb" 会抛 [WinError 2] 系统找不到指定的文件。

    def _resolve_adb(self):
        """解析 adb.exe 路径：优先显式配置，其次 MuMu 自带的那份。"""
        if self.adb_path:
            return self.adb_path
        try:
            from adb_controller import find_mumu_adb
            self.adb_path = find_mumu_adb(None)
        except Exception:
            self.adb_path = "adb"
        return self.adb_path

    def _adb(self, args, timeout=10, retries=2):
        """执行一条 adb 命令并返回 CompletedProcess。

        - 缺 adb 时抛 RuntimeError（而不是让 FileNotFoundError 冒到上层变成
          一句看不懂的 [WinError 2]），并带上解决办法。
        - adb server 冷启动/被杀时第一次调用会失败（stderr 含 "daemon"），
          自动重试。
        """
        exe = self._resolve_adb()
        cmd = [exe] + [str(a) for a in args]
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            except FileNotFoundError:
                raise RuntimeError(
                    f"找不到 adb 可执行文件（当前解析为 {exe!r}）。输入 UID 依赖 adb，"
                    "请在 config.py 把 ADB_PATH 指向 MuMu 自带的 adb.exe，"
                    r"例如 C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe"
                )
            except subprocess.TimeoutExpired:
                last_err = RuntimeError(f"adb 命令超时({timeout}s): {' '.join(str(a) for a in args)}")
                continue
            if r.returncode == 0:
                return r
            err = (r.stderr or b"").decode(errors="ignore").lower()
            out = (r.stdout or b"").decode(errors="ignore").lower()
            # adb daemon 冷启动 / 设备掉线 → 稍等重试
            if any(k in err + out for k in ("daemon", "cannot bind", "offline", "not found")):
                last_err = RuntimeError(
                    f"adb 调用失败(rc={r.returncode}): {err.strip() or out.strip()}"[:200])
                time.sleep(1.2)
                continue
            return r
        raise last_err

    def _adb_device(self, *args, timeout=10):
        """对本实例设备执行 adb 子命令；设备掉线时自动 reconnect 一次再重试"""
        try:
            return self._adb(["-s", self.mumu.serial] + list(args), timeout=timeout)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "not found" in msg or "offline" in msg or "closed" in msg:
                # adb server 重启后设备列表会丢，重新 connect 即可；
                # 但设备卡在 offline 状态时必须先 disconnect 再 connect 才能救回
                self.on_log(f"adb 设备 {self.mumu.serial} 掉线，尝试重连...")
                try:
                    self._adb(["disconnect", self.mumu.serial], timeout=10)
                except RuntimeError:
                    pass
                time.sleep(0.4)
                try:
                    self._adb(["connect", self.mumu.serial], timeout=10)
                except RuntimeError:
                    pass
                time.sleep(0.6)
                return self._adb(["-s", self.mumu.serial] + list(args), timeout=timeout)
            raise

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
            # 动画变体：{name}_b.png（如空旗气泡的星光闪烁帧）存在时一并加载，
            # match() 会逐一尝试，任一命中即算匹配
            vb = self.assets / f"{name}_b.png"
            if vb.exists():
                img = cv2.imread(str(vb), cv2.IMREAD_COLOR)
                if img is not None:
                    self._templates[f"{name}_b"] = img
                    self.on_log(f"  模板 {name}_b: {img.shape[1]}x{img.shape[0]}（动画变体）")

    @staticmethod
    def to_bgr(frame):
        """把截图统一成 3 通道 BGR。

        MuMu 后台截图(nemu_capture_display)返回的是 4 通道 BGRA，
        而模板是用 cv2.imread(..., IMREAD_COLOR) 读进来的 3 通道 BGR。
        cv2.matchTemplate 要求 roi 与模板的 type() 完全一致，
        否则报 (-215) (depth == CV_8U || depth == CV_32F) && type == _templ.type()。
        """
        if frame is None:
            return None
        if frame.ndim == 3 and frame.shape[2] == 4:
            return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        if frame.ndim == 2:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        return frame

    def screenshot(self):
        """后台截图，返回 3 通道 BGR（与模板类型一致）"""
        return self.to_bgr(self.mumu.screenshot())

    def _save_debug_cap(self, prefix, frame):
        """调试现场截图：固定 10 个文件名轮换覆盖写入。

        刻意不做"删旧图"清理——巡访热路径里任何删除操作都可能被
        外部（沙箱/杀软/权限）拦截并阻塞线程，导致整个扫描停摆
        （2026-10-21 实锤）。覆盖写入没有任何删除语义，绝对安全。
        """
        try:
            import os
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_caps")
            os.makedirs(d, exist_ok=True)
            idx = getattr(self, "_cap_idx", 0) + 1
            self._cap_idx = idx
            name = f"{prefix}_{self.mumu.serial.replace(':', '-')}_{idx % 10}.png"
            path = os.path.join(d, name)
            if not cv2.imwrite(path, self.to_bgr(frame)):
                self.on_log(f"警告：{prefix} 截图写入失败: {name}")
        except Exception:
            pass

    def _scale_box(self, box):
        """exe坐标(549x1033)→当前分辨率

        实测游戏画面是按【高度】等比缩放的：1092x1920 屏幕上 search_icon 的
        最佳匹配缩放为 1.85 ≈ 1920/1033，而不是 1092/549=1.99。
        x/y 分开缩放会把 ROI 推偏（x 方向可偏 40px+，导致匹配永远打不中），
        所以必须用统一缩放。
        """
        s = self.height / 1033.0
        x1, y1, x2, y2 = box
        return (int(x1*s), int(y1*s), int(x2*s), int(y2*s))

    def _scale_point(self, x, y):
        s = self.height / 1033.0
        return (int(x*s), int(y*s))

    def match(self, frame, name, override_threshold=None, wide=False):
        """用exe的阈值和box匹配，返回(cx,cy,val)或None

        wide=True 时不按 spec box 圈 ROI，直接全屏搜索——
        用于礼盒/空旗气泡这类"镜头会把它移到画面中心附近"的目标。
        """
        spec = self._specs.get(name)
        if spec is None:
            return None

        # 兜底：无论 frame 来自哪里，都保证与模板同类型（3通道BGR）
        frame = self.to_bgr(frame)

        threshold = override_threshold or spec.get("threshold", 0.88)

        # 动画变体：主模板 + 可选的 {name}_b（如空旗气泡的星光闪烁相位），
        # 逐一尝试取最高分，任一过阈值即命中
        variants = [self._templates[n] for n in (name, f"{name}_b")
                    if n in self._templates]
        if not variants:
            return None

        h, w = frame.shape[:2]
        if wide:
            x1, y1, x2, y2 = 0, 0, w, h
        else:
            box = self._scale_box(spec["box"])
            # ROI：box 向外扩一大圈余量。
            # 实测「拜访」按钮的真实位置比 exe spec 的 box 偏上约 34px，
            # 余量太小会把按钮裁掉一截，TM_CCOEFF_NORMED 分数直接掉到阈值以下。
            mx = max(40, int((box[2]-box[0]) * 0.5))
            my = max(40, int((box[3]-box[1]) * 0.5))
            x1 = max(0, box[0]-mx); y1 = max(0, box[1]-my)
            x2 = min(w, box[2]+mx); y2 = min(h, box[3]+my)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        roi = frame[y1:y2, x1:x2]
        rw = x2 - x1; rh = y2 - y1

        # 全屏搜索（wide）时先降采样一半再匹配：matchTemplate 耗时与像素数成正比，
        # 边长减半 ≈ 4 倍提速；命中坐标除以 ds 还原（实测分数影响 <0.01）。
        # 这是抢礼盒速度的关键——降采样前一次 gift 全屏匹配要 1.7s，根本抢不过别人
        ds = 0.5 if wide else 1.0
        if ds < 1.0:
            roi_m = cv2.resize(roi, None, fx=ds, fy=ds, interpolation=cv2.INTER_AREA)
        else:
            roi_m = roi
        rmw = roi_m.shape[1]; rmh = roi_m.shape[0]

        # 模板按设备缩放（与 _scale_box 同一比例）；若仍大于 ROI，
        # 缩到能放进 ROI（保持宽高比）——绝不因为尺寸差一点就放弃匹配。
        # 礼盒/空旗这类气泡 UI 还有呼吸式缩放动画（实测 ±8%），
        # spec 里可用 "scales" 配多档缩放（如 [1.0, 0.92, 1.08]）
        s = self.height / 1033.0
        mults = spec.get("scales", [1.0])
        cache = getattr(self, "_tpl_cache", None)
        if cache is None:
            cache = self._tpl_cache = {}
        for vname in (name, f"{name}_b"):
            tpl = self._templates.get(vname)
            if tpl is None:
                continue
            th0, tw0 = tpl.shape[:2]
            fit = min(rw / tw0, rh / th0)
            cap = min(s, fit) if fit > 0 else s
            for m in mults:
                scale = cap * m * ds
                key = (vname, round(scale, 3))
                tpl_s = cache.get(key)
                if tpl_s is None:
                    if abs(scale - 1.0) > 0.01:
                        tpl_s = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                    else:
                        tpl_s = tpl
                    cache[key] = tpl_s
                th2, tw2 = tpl_s.shape[:2]
                if th2 >= rmh or tw2 >= rmw:
                    continue
                result = cv2.matchTemplate(roi_m, tpl_s, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val >= threshold:
                    # 命中即返回（抢速度），坐标从降采样图还原到原图
                    cx = x1 + int((max_loc[0] + tw2 / 2) / ds)
                    cy = y1 + int((max_loc[1] + th2 / 2) / ds)
                    return (cx, cy, max_val)
        return None

    def tap(self, x, y, hold_ms=90):
        self.mumu.click(x, y, threading.Event(), lambda p: True, hold_ms=hold_ms)

    def swipe(self, x1, y1, x2, y2, duration_ms=350):
        """滑动手势（滚动手势：手指从 (x1,y1) 拖到 (x2,y2)）"""
        self._adb_device("shell", "input", "swipe",
                         int(x1), int(y1), int(x2), int(y2), int(duration_ms))

    def tap_exe(self, ex, ey, hold_ms=90):
        """按exe坐标点击（自动缩放）"""
        cx, cy = self._scale_point(ex, ey)
        self.tap(cx, cy, hold_ms)

    def input_text(self, text):
        # adb shell input text 里空格要写成 %s
        safe = str(text).replace(" ", "%s")
        self._adb_device("shell", "input", "text", safe)

    def clear_input(self, n=12):
        """清空输入框：**一次 adb 调用连发 n 个删除键**。

        原来每个删除键发一次 adb（15 次进程启动 ≈ 1 秒，是整个搜索环节
        最慢的一步）。Android 的 `input keyevent` 支持一次传多个键码，
        合并成一条命令后只有一次进程开销（≈60ms）。
        """
        try:
            self._adb_device("shell", "input", "keyevent", *(["67"] * n), timeout=8)
        except Exception:
            # 个别机型不支持多键码参数 → 退回逐个删除
            for _ in range(n):
                self._adb_device("shell", "input", "keyevent", "67", timeout=5)

    def key_event(self, keycode=66):
        """发送按键：66=回车，4=返回"""
        self._adb_device("shell", "input", "keyevent", str(keycode))

    def wait(self, seconds):
        time.sleep(seconds)

    def _wait_template(self, name, timeout=2.0, interval=0.12, wide=False):
        """轮询等某个模板出现，返回 (pos, frame)。

        比"固定 sleep 再截图"快得多：按钮一出现就立刻返回，
        通常 0.2~0.5s（原来不管快慢都要等满固定时间）。
        """
        start = time.time()
        frame = None
        while True:
            frame = self.screenshot()
            pos = self.match(frame, name, wide=wide)
            if pos:
                return pos, frame
            if time.time() - start >= timeout:
                return None, frame
            time.sleep(interval)

    def _result_fingerprint(self, frame):
        """搜索结果卡区域（头像+名字）的粗指纹，用于判断结果是否已刷新。"""
        frame = self.to_bgr(frame)
        h, w = frame.shape[:2]
        y1, y2 = int(1150 / 1920 * h), int(1520 / 1920 * h)
        x1, x2 = int(50 / 1092 * w), int(720 / 1092 * w)
        g = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (24, 24), interpolation=cv2.INTER_AREA)
        return g.astype("int16")

    def _wait_result_refresh(self, before, timeout=2.5, interval=0.15, thresh=6.0):
        """等搜索结果刷新（卡片区域画面变化）。返回 True=检测到变化。

        替代原来固定 wait(2.5)：游戏响应快时 0.3s 就返回。
        """
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(interval)
            f = self.screenshot()
            diff = float(abs(self._result_fingerprint(f) - before).mean())
            if diff > thresh:
                return True
        return False

    def _wait_result_stable(self, timeout=1.5, interval=0.12, need_same=2):
        """等结果卡入场动画停住（连续两次指纹基本一致）。

        结果卡是从下方滑入的：动画没停就点头像会点在空位置上，
        点击被吞掉后要等两秒才能靠补点补救 —— 先等停稳再点更快更稳。
        """
        prev = self._result_fingerprint(self.screenshot())
        same = 0
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(interval)
            cur = self._result_fingerprint(self.screenshot())
            if float(abs(cur - prev).mean()) < 1.5:
                same += 1
                if same >= need_same:
                    return True
            else:
                same = 0
            prev = cur
        return False

    def wait_for_state(self, target_states, timeout=12, interval=0.15):
        """轮询等待直到出现目标状态，返回(state, frame)或(None, None)超时。
        轮询间隔 0.15s：比原来的 0.3s 响应更快（截图本身约 30ms，够便宜）"""
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

    def wait_for_home(self, timeout=10, interval=0.2, dwell=None):
        """等待进入家园 → 等镜头位移 → 判定礼盒状态。

        返回 (state, frame) 或 (None, None)：
        - 先轮询「是否已进入对方家园」（右下角点赞 thumb / 礼盒 / 荣誉墙）
        - 进入后镜头要 2~3 秒才位移到礼盒位置，截太早会看到"既没礼盒也没
          空旗气泡"的画面而误判。所以进入后**最多等 dwell 秒（默认3s）**，
          期间每 0.2s 轮询一次：一旦看到礼盒/荣誉墙/空旗气泡立刻判定返回，
          不用干等满（多数情况 1~2 秒就能出结果）。
        - 到时间还没任何标志 → 判定 home_empty（没礼盒）。
        """
        if dwell is None:
            dwell = self.home_dwell
        start = time.time()
        entered = False
        while time.time() - start < timeout:
            frame = self.screenshot()
            if self.match(frame, "gift", wide=True):
                return "home_gift", frame
            if self.match(frame, "honors"):
                return "honors", frame
            # 空旗气泡可见 → 已进入空家园（礼盒被抢完），直接判定，无需等镜头
            if self.match(frame, "empty_flag", wide=True):
                return "home_empty", frame
            if self.match(frame, "thumb"):
                entered = True
                # 先手点击：进家园瞬间镜头就把礼盒移到屏幕中心附近
                # （实测 (296,517) exe ≈ 屏幕中心），不等气泡识别直接先点一下，
                # 抢礼盒就是拼这一下。点完继续轮询，若面板弹出会在下方检测到。
                if self.pre_tap_gift and not self._pre_tapped:
                    self._pre_tapped = True
                    self.on_log("进家园先手点击礼盒位置（不等气泡识别）")
                    self.tap_exe(*self.GIFT_CENTER_EXE, hold_ms=120)
                break
            other = self.detect_state(frame)
            if other in ("claimed", "choose"):
                return other, frame
            time.sleep(interval)
        if not entered:
            return None, None

        # 已进入家园：轮询等镜头移到礼盒位置（最多 dwell 秒，看到标志立刻返回）
        self.on_log(f"已进入家园，最多等 {dwell:.1f}s 看镜头是否移到礼盒位置")
        deadline = time.time() + dwell
        frame = None
        while time.time() < deadline:
            frame = self.screenshot()
            if self.match(frame, "gift", wide=True):
                self.on_log("看到礼盒（有可领取礼盒）")
                return "home_gift", frame
            if self.match(frame, "honors"):
                return "honors", frame
            if self.match(frame, "empty_flag", wide=True):
                self.on_log("看到空旗气泡（已抢完）")
                return "home_empty", frame
            # 先手点击可能已弹出选择祝福/结果面板（此时礼盒气泡不可见）
            st = self.detect_state(frame)
            if st in ("choose", "claimed"):
                self.on_log(f"先手点击已生效（{st}）")
                return st, frame
            time.sleep(interval)
        if frame is None:
            frame = self.screenshot()
        return "home_empty", frame

    def tap_gift_multi(self, times=5, interval=0.35, timeout=8):
        """礼盒气泡出现时，连续点击 4~5 次，再等待弹窗。

        游戏里单次点击礼盒经常不响应（动画/点击判定问题），所以连续点几次。
        每次点击前都重新截图定位礼盒（气泡随镜头/动画移动），并且先判断
        是否已经弹出弹窗（choose/claimed/honors）——已弹出就不再继续点，避免误触。
        实测：礼盒已被领完的家园，点礼盒会直接打开「荣誉墙」（50人已祝福），
        所以 honors 也是有效出口。
        连续两次找不到礼盒就停止补点。

        返回 (state, frame)，失败返回 (None, None)。
        """
        exit_states = ("choose", "claimed", "honors")
        tapped = 0
        missed = 0
        for i in range(times):
            frame = self.screenshot()
            # 先匹配礼盒（宽域、快）：礼盒还在说明弹窗肯定没出，
            # 不用跑一遍 detect_state（全套近 3 秒，抢礼盒等不起）
            pos = self.match(frame, "gift", wide=True)
            if not pos:
                st = self.detect_state(frame)
                if st in exit_states:
                    self.on_log(f"礼盒点击已生效（{st}），停止补点")
                    return st, frame
            if pos:
                tapped += 1
                missed = 0
                self.on_log(f"点击礼盒（第{tapped}/{times}次）匹配度 {pos[2]:.2f}")
                self.tap(pos[0], pos[1])
            else:
                missed += 1
                self.on_log(f"礼盒未找到（第{i+1}/{times}次），跳过补点")
                if missed >= 2:
                    break
            self.wait(interval)
        return self.wait_for_state(exit_states, timeout=timeout)

    def _maybe_return(self, why=""):
        """visit_one(stay=True) 时领完停在家园，不自动返回找朋友页。"""
        if getattr(self, "_stay", False):
            self.on_log(f"停留在家园（{why}），不自动返回，由用户手动退出")
            return
        self._return_to_find_friends()

    def _handle_empty_home(self):
        """家园无礼盒：空旗气泡已确认，或观察窗口内未见礼盒。
        2026-10-21 起游戏会在进家园 ~3s 后自动退出（疑似活动期行为），
        观察窗口内没看到礼盒按空家园处理。stay 模式停在家园。"""
        self.on_log("家园无礼盒（空旗或未观察到）")
        self._maybe_return("空旗家园")
        return {"status": "empty", "detail": "empty_flag_confirmed"}

    def _screen_fingerprint(self, frame):
        """整屏小指纹（灰度缩到 64x36），用于"点击是否让画面变化了"的判断。

        实测：头像点击被输入锁吞掉时画面**零变化**；点击生效（资料弹窗打开）
        时画面大面积变化。用平均绝对差区分这两种情况。
        """
        g = cv2.cvtColor(self.to_bgr(frame), cv2.COLOR_BGR2GRAY)
        return cv2.resize(g, (64, 36), interpolation=cv2.INTER_AREA).astype("float32")

    def tap_visit_and_wait_home(self, max_retries=4, timeout=8):
        """点搜索结果卡的头像 → 等「拜访」按钮 → 进入家园。

        实测三个关键点：
        - 搜索刚结束有约 0.7s 输入锁，这期间点头像会被完全吞掉（画面零变化）；
        - 资料页弹窗 0.4~0.6s 浮现；
        - **弹窗开着时绝不能再点头像**——头像位置落在弹窗外的暗色遮罩上，
          再点一下会把弹窗直接关掉（用户实测反馈："点到拜访之外取消了"）。

        所以没等到「拜访」按钮时，用**整屏指纹变化**区分两种情况：
        - 画面几乎没变 → 点击被吞了 → 可以再点头像；
        - 画面明显变化 → 点击已生效（弹窗开着，只是模板没匹配到/动画中）
          → 只补点拜访按钮，**绝不再碰头像**。
        """
        avatar = (92, 660)  # 搜索结果卡上的头像（exe 坐标）
        spec = self._specs.get("visit")
        if spec:
            b = spec["box"]
            visit_exe = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        else:
            visit_exe = (160, 1015)

        popup_open = False
        base_fp = None
        tapped_y = None
        for attempt in range(1, max_retries + 1):
            if not popup_open:
                if attempt == 1:
                    # 第一下前等两件事：结果卡滑入动画停住 + 搜索处理完成。
                    # 实测点放大镜后 ~0.7s 内游戏还在处理搜索（输入锁），
                    # 这期间点头像会被完全吞掉，所以固定搜索后 ~0.9s 再点第一下。
                    self._wait_result_stable()
                    self.wait(0.6)
                base_fp = self._screen_fingerprint(self.screenshot())
                self.on_log(f"点击头像（第{attempt}次）")
                self.tap_exe(*avatar, hold_ms=130)

            # 等「拜访」按钮（正常 0.4~0.6s 就出现）。
            # ⚠️ 必须 wide=True 全屏搜索：实测跑一段时间后这个按钮在弹窗里的
            # 位置会往下挪（弹窗内容变高把它顶下去），按 spec box 圈的小 ROI
            # 会直接搜不到 → 一路失败。按钮样式独特，全屏搜索不会误报。
            pos, frame = self._wait_template("visit", timeout=1.3, interval=0.1, wide=True)
            if pos:
                popup_open = True
                # 用户实测反馈：弹窗弹出后点太快会点空 → 固定等 0.5s 再点
                self.wait(0.5)
                # 等完重新定位一次（按钮若有微动取最新位置）
                p2, frame = self._wait_template("visit", timeout=1.0, interval=0.1, wide=True)
                if p2:
                    pos = p2
                self.on_log(f"点击拜访（第{attempt}次）匹配度 {pos[2]:.2f} @({pos[0]},{pos[1]})")
                # 现场截图：排查"匹配到却点不进"——看清匹配位置到底是不是按钮。
                # ⚠️ 不要在巡访热路径里 os.remove 清理旧图：删除操作可能被外部
                # 拦截并无限阻塞（2026-10-21 实锤：线程卡死在删除上，整个扫描
                # 停摆）。改用固定文件名轮换覆盖写入，永不删除。
                self._save_debug_cap("visit_before", frame)
                # 实测（2026-10-21 受控实验）：按钮即使被顶到底缘 y=1874，
                # 直接点模板中心也能正常进家园，不要偏移、不要滑动
                tapped_y = pos[1]
                self.on_log(f"点击拜访 @({pos[0]},{tapped_y})")
                self._visit_clicked = True
                self.tap(pos[0], tapped_y, hold_ms=130)   # 按久一点，慢实例上短按容易不响应
            else:
                diff = 999.0
                if base_fp is not None:
                    diff = float(abs(self._screen_fingerprint(frame) - base_fp).mean())
                if diff < 6.0:
                    # 画面几乎没变 → 这一次点击被吞了，可以再点头像
                    popup_open = False
                    self.on_log(f"资料页未弹出（第{attempt}次，画面几乎无变化），继续点头像")
                    continue
                # 画面变了但「拜访」按钮已经不在 → 界面跑飞了（实测会点进玩法页），
                # 不能再按坐标盲点：盲点正是"回不到好友界面"的根源，直接中止
                self.on_log(f"画面已变化（差异 {diff:.0f}）但拜访按钮消失，停止补点避免乱点")
                self._save_debug_cap("visit_lost", frame)
                return None, None

            state, frame = self.wait_for_home(timeout=timeout)
            if state is not None:
                return state, frame

            # 兜底：按钮停稳位置在 1804~1874 之间漂移（弹窗内容高度不同），
            # 匹配到也可能点空 → 在按钮可见区间内小范围上下补点（y 必须留在
            # height-90 以上：再往下就进底部✕横条，横条会吃掉点击）
            if pos and tapped_y is not None:
                for dy in (-25, 25):
                    ty2 = min(max(tapped_y + dy, 60), self.height - 90)
                    self.on_log(f"拜访补点 {dy:+d}px @({pos[0]},{ty2})")
                    self.tap(pos[0], ty2, hold_ms=120)
                    state, frame = self.wait_for_home(timeout=timeout)
                    if state is not None:
                        return state, frame

            self.on_log(f"拜访未响应，{'重试' if attempt < max_retries else '放弃'}")
        # 彻底失败：存一张现场截图，便于事后排查（轮换覆盖，不删除）
        self._save_debug_cap("visit_fail", self.screenshot())
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

        # claimed: 领取结果页（best_title = "当前手气最佳"）
        if self.match(frame, "best_title"):
            return "claimed"

        # choose: 选择祝福弹窗（choice_title = "选择祝福"）
        if self.match(frame, "choice_title"):
            return "choose"

        # results: 搜索结果页（results_title = "搜索结果"）
        # 注：suggest_title("可能认识的玩家")在找朋友页面也显示，不能作为判定依据。
        # 标题类判定要放在图标类之前——搜索结果页可能还残留搜索框，会被 search_icon 抢先命中。
        if self.match(frame, "results_title"):
            return "results"

        # home_gift: 发现礼盒（位置随镜头移动，全屏搜索）
        if self.match(frame, "gift", wide=True):
            return "home_gift"

        # search: 找朋友页面（搜索图标）
        if self.match(frame, "search_icon"):
            return "search"

        return "unknown"

    def _is_home_page(self, frame):
        """是否在（别人的）家园页面。

        判定规则（用户确认的游戏机制）：
        - 右下角点赞按钮(thumb) = 确认已进入对方家园
        - gift（粉礼盒）= 有礼盒可领；空旗气泡（empty_flag，白气泡+红金相框猫）
          = 礼盒已被抢完。这两种标志不是每个家园都显示，只在部分家园出现
          （进家园后镜头移到礼盒位置时才显示，约 2~3 秒）。
        - 所以：看到 gift → home_gift；看到 thumb 且无 gift → home_empty
          （empty_flag 出现时直接判 home_empty，作为正向证据）。

        礼盒/空旗气泡位置随镜头移动，用 wide=True 全屏搜索。
        """
        if self.match(frame, "gift", wide=True):
            return "home_gift"
        if self.match(frame, "honors"):
            return "honors"
        # 空旗气泡可见 = 已确认在空家园，比 thumb 更直接的证据
        if self.match(frame, "empty_flag", wide=True):
            return "home_empty"
        if self.match(frame, "thumb"):
            # 已确认在对方家园。镜头可能还没移到礼盒位置，稍等再确认一次
            self.wait(self.delay)
            frame2 = self.screenshot()
            if self.match(frame2, "gift", wide=True):
                return "home_gift"
            if self.match(frame2, "honors"):
                return "honors"
            return "home_empty"
        return None

    def count_player_cards(self, frame=None):
        """统计面板上玩家卡片的数量（按绿色「申请」按钮计数）。

        用户确认的判定规则：搜索结果里**只有 1 个人才是正确的**；
        UID 输错/不存在时游戏弹「请输入正确的玩家名字或数字编号」并留在
        找朋友页，列表显示「可能认识的玩家」多个推荐 → 数出 ≥2 个。

        用亮绿色按钮做连通域计数；头像上的绿色装饰（约70x90、面积仅1500）
        会被尺寸阈值过滤掉（真按钮约190x108、面积14500 @1092x1920）。
        """
        if frame is None:
            frame = self.screenshot()
        frame = self.to_bgr(frame)
        h, w = frame.shape[:2]
        k = h / 1920.0  # 阈值按 1092x1920 基准缩放
        # 列表区域从输入框下方开始，避开顶部头像/装扮的绿色元素
        roi = frame[int(1152 / 1920.0 * h):, :]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (35, 120, 120), (85, 255, 255))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        cnt = 0
        for i in range(1, n):
            x, y, bw, bh, a = stats[i]
            if a > 5000 * k * k and bw > 120 * k:
                cnt += 1
        return cnt

    def _wait_search_result(self, timeout=12, interval=0.15):
        """搜索后轮询等待结果。

        返回 (state, frame)：
        - state ∈ results/home_gift/home_empty/honors/claimed → 正常流程
        - state == "not_found" → UID 不存在（空推荐列表 或 多个玩家）
        - (None, None) → 超时，交给上层盲点头像重试
        """
        start = time.time()
        empty_since = None
        while time.time() - start < timeout:
            f = self.screenshot()
            st = self.detect_state(f)
            if st in ("results", "home_gift", "home_empty", "honors", "claimed"):
                return st, f
            elapsed = time.time() - start
            # 无效 UID（改版后）：游戏不再推荐一堆人，而是停在找朋友页给出
            # 「可能认识的玩家 + 暂时没有内容(幽灵图)」的空列表（empty_list 模板）。
            # 必须等满 3s 再认：排除搜索请求还在路上 / 旧列表残留；
            # 且要求连续两帧（间隔≥0.4s）都命中，防止把"结果还没加载出来"误判成不存在。
            # （没有这个判定时，错误 UID 会干等 12s 超时再盲点头像 3 次，一个坏 UID 卡 30s）
            if elapsed > 3.0:
                if self.match(f, "empty_list"):
                    if empty_since is None:
                        empty_since = time.time()
                    elif time.time() - empty_since >= 0.4:
                        return "not_found", f
                else:
                    empty_since = None
            # 无效 UID（旧行为）：弹 toast 后留在找朋友页，推荐列表有多个人
            if elapsed > 3.5 and self.count_player_cards(f) >= 2:
                return "not_found", f
            time.sleep(interval)
        return None, None

    # ── 巡访主流程 ──

    def visit_one(self, uid, scan_only=False, timeout=30, stay=False):
        """
        完整巡访流程，复用exe状态机。
        search → results → profile → home_gift/home_empty → choose → claimed → 返回search

        :param scan_only: True=只查找不领取（小号模式），发现可领取礼盒→返回available
                          False=查找并领取（主号模式），完整流程
        :param stay: True=领完礼盒（或确认空旗）后**停在家园不返回**，由用户手动退出；
                     下次巡访开头会自动先返回找朋友页再搜下一个 UID
        """
        self.on_log(f"[{self.mumu.serial}] 开始巡访 UID: {uid} {'（只查找模式）' if scan_only else ''}{'（领完停留家园）' if stay else ''}")
        self._stay = stay
        self._visit_clicked = False
        # 横屏防护：坐标换算基于竖屏 exe 版式（584x1033）按高度等比缩放，
        # 横屏（如 1920x1080）下所有点击都会严重偏移（乱点开无关菜单），
        # 必须直接拒绝并提示用户把模拟器转回竖屏
        if self.width > self.height:
            self.on_log(f"[{self.mumu.serial}] ✗ 实例是横屏（{self.width}x{self.height}），"
                        f"竖屏坐标换算全部失效。请把该模拟器转回竖屏（分辨率如 1080x1920）后重试")
            return {"status": "error", "detail": "landscape_screen"}
        # 主号领取模式才用"进家园先手点击礼盒"；小号查找不许替主号点开面板
        self.pre_tap_gift = not scan_only
        self._pre_tapped = False

        # 步骤1：确认处于「可输入 UID」的面板（找朋友页 或 搜索结果页都行）
        state, frame = self.wait_for_state(("search", "results"), timeout=5)
        if state is None:
            self.on_log("不在找朋友页面，尝试返回")
            self._return_to_find_friends()
            state, frame = self.wait_for_state(("search", "results"), timeout=5)
            if state is None:
                return {"status": "error", "detail": "open_find_friends_first"}
        # 上一次搜索的旧结果还在面板上，先清空输入框（下面 clear_input 会处理），
        # 但要记住这一点：搜索后必须等新结果刷新，不能立刻把旧结果当成新的。
        started_from_results = (state == "results")

        # 步骤2：点击输入框，输入UID，搜索
        # 注意：必须点输入框本身，点右侧的放大镜图标没有任何反应（实测）。
        # 输入框中心在 exe 坐标 (276, 559)，放大镜图标在 (532, 533)。
        t_search = time.time()
        self.on_log("点击输入框")
        self.tap_exe(276, 559)
        self.wait(0.15)  # 只要等输入框获得焦点即可，不必等满 delay

        self.on_log(f"输入UID: {uid}")
        self.clear_input()          # 一次 adb 调用批量删除
        self.input_text(uid)
        self.wait(0.12)

        # 点放大镜前先记下结果卡指纹（用于判断新结果是否已刷新）
        before_fp = self._result_fingerprint(self.screenshot())

        # 回车不会触发搜索（实测：输入 UID 后按 Enter 毫无反应），
        # 必须点击输入框右侧的放大镜图标
        self.on_log("点击放大镜执行搜索")
        frame = self.screenshot()
        pos = self.match(frame, "search_icon")
        if pos:
            self.tap(pos[0], pos[1])
        else:
            self.tap_exe(532, 533)

        # 面板上还挂着上一次的搜索结果时，必须等新结果刷新，
        # 否则会把旧结果当成新搜索的结果（点错人的头像）。
        # 用指纹对比自适应等待：快时 0.3s，慢时最多 2.5s（原来固定 2.5s）。
        if started_from_results:
            refreshed = self._wait_result_refresh(before_fp, timeout=2.5)
        else:
            self.wait(0.25)
            refreshed = True
        # 保险：结果区域完全没变化 → 第一次点击可能被输入法/动画吃掉，补点一次
        if not refreshed:
            self.on_log("搜索结果未刷新，补点一次放大镜")
            pos = self.match(self.screenshot(), "search_icon")
            if pos:
                self.tap(pos[0], pos[1])
                self._wait_result_refresh(before_fp, timeout=2.0)
        self.on_log(f"输入+搜索完成，耗时 {time.time()-t_search:.1f}s")

        # 步骤3：轮询等待搜索结果（timeout=12，和exe一致）
        # 搜索后可能出现：results(搜索结果)、home_gift/home_empty(直接进家园)、honors(荣誉墙)、claimed(已领过)
        # 另外：UID 不存在时游戏会弹「请输入正确的玩家名字或数字编号」并停在
        # 找朋友页显示「可能认识的玩家」多个推荐 —— 列表里有多个人 = 无效 UID
        #（用户规则：搜索结果里只有 1 个人才是正确的）。
        state, frame = self._wait_search_result(timeout=12)
        if state == "not_found":
            self.on_log(f"搜索结果有多个玩家（应只有1个），UID {uid} 不存在，跳过")
            return {"status": "not_found", "detail": "uid_not_found"}
        if state is None:
            # 搜索超时，可能结果页出来了但标题没识别到，盲点头像位置
            self.on_log("搜索后未识别到状态，尝试点击头像位置")
            state, frame = self.tap_visit_and_wait_home(max_retries=3, timeout=6)
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
            self._maybe_return("荣誉墙")
            return {"status": "empty", "detail": "honors_without_gift"}

        if state == "home_empty":
            return self._handle_empty_home()

        if state == "results":
            # 步骤4：搜索结果页 → 点头像打开资料页 → 点「拜访」进家园
            # 实测：资料页是弹窗，顶部搜索栏仍可见，detect_state 会误报 "search"，
            # 所以不能靠状态等待，直接用 visit 模板定位「拜访」按钮。
            # 头像在结果卡左侧，exe 坐标 (92, 660)（原 523,853 是页面空白处）。
            # 头像在结果卡左侧，exe 坐标 (92, 660)。
            # 点头像 → 等「拜访」按钮，由 tap_visit_and_wait_home 内部完成：
            # 先等结果卡停稳，点一次头像后只等 0.55s，没弹窗立刻再点，
            # 直到资料页弹出（搜索后的输入锁会吞掉第一下，靠快速补点补回来）
            state, frame = self.tap_visit_and_wait_home(
                max_retries=4, timeout=8)
            if state is None:
                self.on_log("点击拜访后仍未进入家园")
                self._return_to_find_friends()
                return {"status": "error", "detail": "profile_timeout"}

        # 步骤6续：在家园页面处理礼盒
        if state == "choose":
            # 先手点击（进家园立刻点礼盒位置）直接弹出了选择祝福面板
            self.on_log("先手点击已弹出选择祝福面板，直接收尾")
            res, frame = self._finish_claim(frame)
            if res == "claimed":
                return {"status": "claimed", "detail": "blessing_chosen"}
            self.on_log("领取收尾失败，尝试标准返回")
            self._return_to_find_friends()
            return {"status": "error", "detail": "claim_finish_failed"}

        if state == "home_gift":
            # scan_only模式：发现可领取礼盒，不点击，直接返回
            if scan_only:
                self.on_log("发现可领取礼盒（只查找模式），不领取" +
                            ("，停留在家园" if getattr(self, "_stay", False) else "，返回找朋友"))
                self._maybe_return("只查找")
                return {"status": "available", "detail": "gift_icon_detected"}

            # 正常模式：礼盒气泡出现 → 连续点击 4~5 次（单次点击常不响应）
            state, frame = self.tap_gift_multi(times=5, interval=0.35, timeout=8)
            if state is None:
                # 连点多次礼盒没响应，检查是否已抢完
                self.on_log("礼盒点击未响应，检查是否已抢完")
                frame = self.screenshot()
                home_state = self._is_home_page(frame)
                if home_state == "home_empty":
                    self.on_log("确认空旗（礼盒已消失）")
                    self._maybe_return("礼盒已消失")
                    return {"status": "empty", "detail": "gift_no_longer_available"}
                else:
                    self.on_log("礼盒仍在但点击无效，标记需复核")
                    self._maybe_return("礼盒点击无效")
                    return {"status": "error", "detail": "gift_click_not_accepted"}

            if state == "honors":
                self.on_log("点礼盒后进入荣誉墙（礼盒已被领完，如50人已祝福）")
                self._maybe_return("荣誉墙")
                return {"status": "empty", "detail": "honors_after_gift"}

            if state == "choose":
                res, frame = self._finish_claim(frame)
                if res == "claimed":
                    return {"status": "claimed", "detail": "blessing_chosen"}
                self.on_log("领取收尾失败，尝试标准返回")
                self._maybe_return("领取收尾失败")
                return {"status": "error", "detail": "claim_finish_failed"}

        elif state == "home_empty":
            return self._handle_empty_home()

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
                self.on_log("发现礼盒（重试），连续点击领取")
                state, frame = self.tap_gift_multi(times=5, interval=0.35, timeout=8)
                if state == "honors":
                    self.on_log("点礼盒后进入荣誉墙（礼盒已被领完）")
                    self._maybe_return("荣誉墙")
                    return {"status": "empty", "detail": "honors_after_gift"}
                if state == "choose":
                    res, frame = self._finish_claim(frame)
                    if res == "claimed":
                        return {"status": "claimed", "detail": "blessing_chosen"}
                    self.on_log("领取收尾失败，尝试标准返回")
                    self._maybe_return("领取收尾失败")
                    return {"status": "error", "detail": "claim_finish_failed"}
            elif state == "home_empty":
                return self._handle_empty_home()
            elif state == "choose":
                res, frame = self._finish_claim(frame)
                if res == "claimed":
                    return {"status": "claimed", "detail": "blessing_chosen"}
                self.on_log("领取收尾失败，尝试标准返回")
                self._return_to_find_friends()
                return {"status": "error", "detail": "claim_finish_failed"}
            else:
                self.on_log(f"家园状态仍未确认: {state}")
                self._return_to_find_friends()
                return {"status": "error", "detail": f"home_state={state}"}

        # 步骤7：等待领取结果页（点礼盒后直接弹结果页的情况走这里；
        # 选择祝福面板的收尾已由 _finish_claim 内部处理并直接 return）
        if state != "claimed":
            state, frame = self.wait_for_state("claimed", timeout=10)

        if state == "claimed":
            self.on_log("领取结果页，领取成功")
            self._close_result(frame)
            self._maybe_return("领取成功")
            return {"status": "claimed", "detail": "gift_icon_detected"}

        # 最后检查：可能礼盒已不可领取
        frame = self.screenshot()
        state = self.detect_state(frame)
        if state == "home_gift":
            self.on_log("礼盒仍在，可能已不可领取")
            self._maybe_return("礼盒不可领取")
            return {"status": "empty", "detail": "gift_no_longer_available"}
        if state == "home_empty":
            return self._handle_empty_home()

        self.on_log(f"巡访结束，最终状态: {state}")
        self._return_to_find_friends()
        return {"status": "error", "detail": f"final_state={state}"}

    def _finish_claim(self, frame=None):
        """领取收尾（用户确认的通用流程，2026-10-14）：
        循环直到回到好友搜索页：
        1. 画面上只要出现绿色按钮（「选择」或「好的」）就点一下
        2. 每点一次就点一下面板下方的X，试试能否退回好友搜索页
        3. 没退回就再来一轮（弹窗可能是：选择祝福面板 / 手气最佳结果页）

        返回 ("claimed", frame)=已回到可输入下一个UID的页面；(None, frame)=超时失败。
        """
        self.on_log("领取收尾：绿色按钮(选择/好的) → 下方X 循环")
        deadline = time.time() + 25
        btn_labels = {"choose_button": "选择", "ok": "好的"}
        while time.time() < deadline:
            # 1) 有绿色按钮就点（同一画面不会同时出现「选择」和「好的」）
            tapped = None
            for name, label in btn_labels.items():
                pos = self.match(frame, name, wide=True)
                if pos:
                    self.on_log(f"点击「{label}」按钮（匹配度 {pos[2]:.2f}）")
                    self.tap(pos[0], pos[1])
                    tapped = label
                    self.wait(0.6)   # 等弹窗切换动画
                    break

            # 2) 点一下下方的X试试退出（结果页/选择面板底部都是这个X）
            frame = self.screenshot()
            xpos = self.match(frame, "close", wide=True)
            if xpos:
                self.on_log(f"点击下方X（匹配度 {xpos[2]:.2f}）")
                self.tap(xpos[0], xpos[1])
                self.wait(0.9)

            # 3) 检查是否已回到好友搜索/搜索结果页
            frame = self.screenshot()
            # stay 模式：关掉结果弹窗、回到对方家园就停（家园留给用户手动退出）
            if getattr(self, "_stay", False) and self._is_home_page(frame):
                self.on_log("领取完成，停留在家园（stay 模式）")
                return "claimed", frame
            if self._ready_for_next_uid(frame):
                self.on_log("已返回好友搜索页，领取完成")
                return "claimed", frame

            # 4) 绿色按钮和X都没匹配到：可能已经退回到对方家园，
            #    走标准返回路径（点家园底部X等）
            if not tapped and not xpos:
                if getattr(self, "_stay", False) and self._is_home_page(frame):
                    self.on_log("领取完成，停留在家园（stay 模式）")
                    return "claimed", frame
                self._return_to_find_friends()
                frame = self.screenshot()
                if self._ready_for_next_uid(frame):
                    self.on_log("已返回好友搜索页，领取完成")
                    return "claimed", frame

        self.on_log("领取收尾超时：25s 内未能返回好友搜索页")
        return None, self.screenshot()

    def _close_result(self, frame):
        """关闭结果页"""
        self.on_log("关闭结果页")
        # wide=True：X 按钮的实际位置可能偏离 spec box（不同弹窗/页面布局不同）
        pos = self.match(frame, "close", wide=True)
        if pos:
            self.tap(pos[0], pos[1])
        else:
            pos = self.match(frame, "ok")
            if pos:
                self.tap(pos[0], pos[1])
            else:
                self.tap_exe(292, 1000)  # close box中心
        self.wait(1)

    # 主界面右下角「好友」图标的 exe 坐标，点它打开找朋友面板。
    # 2026-10-21 实测新版主界面图标列在屏幕 x≈990：聊天(990,1340)/任务(990,1465)
    # /好友双小人(990,1600) → exe = 屏幕/1.858
    FRIEND_ENTRY_EXE = (533, 861)
    # 礼盒气泡的屏幕位置：进家园后镜头会把礼盒移到屏幕中心附近。
    # 实测礼盒气泡中心在 1092x1920 上是 (551,961) → exe 坐标 (296,517)。
    GIFT_CENTER_EXE = (296, 517)

    def _ready_for_next_uid(self, frame):
        """是否已处于「可输入下一个 UID」的面板：
        - search：找朋友页（推荐列表）
        - results：搜索结果页（输入框 + 放大镜都还在，可直接搜下一个）
        资料弹窗开着时 detect_state 会误报 search，必须先排除。
        """
        # wide=True：拜访按钮位置会随弹窗内容下移，必须全屏找（见 tap_visit_and_wait_home）
        if self.match(frame, "visit", wide=True):
            return False
        st = self.detect_state(frame)
        if st == "search":
            return True
        if st == "results" and self.match(frame, "search_icon"):
            return True
        return False

    def _return_to_find_friends(self, timeout=20):
        """回到可输入 UID 的找朋友面板 / 搜索结果面板。

        实测路径（不要用 Android BACK 键，它在游戏里基本无效，
        在面板上乱按反而会把面板关掉退到游戏主界面）：
        - 别人家园 → 点底部 X 关闭按钮 → 回到搜索结果面板（输入框还在）→ 完成
        - 领取结果页弹窗 → 点关闭/确定 → 回家园 → 再点 X
        - 游戏主界面（跑偏了）→ 点右下角「好友」图标打开面板
        """
        self.on_log("返回找朋友页面")
        start = time.time()
        while time.time() - start < timeout:
            frame = self.screenshot()
            if self._ready_for_next_uid(frame):
                return True

            # 资料弹窗还开着（上次中途失败）→ 点弹窗外部关掉
            # （实测 Android BACK 关不掉这个弹窗）
            if self.match(frame, "visit", wide=True):
                self.tap_exe(275, 20)
                self.wait(1.2)
                continue

            # 在家园 / 领取结果页 / 玩法菜单等 → 点关闭按钮（全屏找，
            # 玩法页的 X 位置比 spec box 偏右下，小范围匹配会漏）
            pos = self.match(frame, "close", wide=True) or self.match(frame, "ok", wide=True)
            if pos:
                self.tap(pos[0], pos[1])
                self.wait(1.5)
                continue

            # 未知画面（游戏主界面等）→ 点右下角好友入口
            self.on_log("画面未识别，点击主界面好友入口")
            self.tap_exe(*self.FRIEND_ENTRY_EXE)
            self.wait(1.8)

        ok = self._ready_for_next_uid(self.screenshot())
        if not ok:
            self.on_log("警告：未能回到找朋友页面")
        return ok
