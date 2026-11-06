# -*- coding: utf-8 -*-
"""
总攻自动参加监控（海岛夺金活动）—— 驻留聊天页版（2026-11-06 重构）

与旧版的区别：监控驻留点从「活动地图」改为「聊天窗页」。
旧版每轮都关聊天回地图看红点；新版没参加总攻时一直停在聊天页轮询，
新总攻卡片带绿色「参与」条会自动刷出来，直接点。

流程（每个实例独立执行）：
  1. 清空「总攻完成!」弹框（每轮开头 + 每次回到总攻页后，可能有多个，循环处理）
  2. 页面判定：
     - 聊天窗页 → 直接进入监听（驻留态，不关闭）
     - 总攻页（活动地图）→ 点右下角聊天入口进入聊天窗页
     - 停在总攻详情/列表页（上轮没退出）→ 立即按返回恢复
     - 其他未知页面 → miss 计数，连续约60秒触发自愈
  3. 聊天窗页：扫描绿色「参与」按钮条（灰「已申请/申请中/已过期」天然跳过）
     - 没有 → 继续停留，等下一轮（聊天页自动刷新新消息）
     - 有 → 点最底部（最新）一条
  4. 进入总攻页（两种形态，按底部条颜色占比区分）：
     - 详情页：底部绿按钮 >25% → 点 (0.58w, 0.934h) 参加；灰 → 已参与跳过
     - 列表页（同地块多条总攻）：完整子循环——
       扫描右侧按钮列（颜色+模板）→ 逐个点绿「参与」→ 本屏点完上滑一屏
       → 滑动后画面指纹不变且无新绿按钮 = 到底 → 结束
       （点击没生效的按钮重扫时会补点；上限 40 次点击 / 12 次翻页防死循环）
  5. 点左下角返回 → 回到总攻页（若实际落在聊天页则自适应点X）
  6. 回到总攻页后再清一轮「总攻完成!」弹框 → 重新点进聊天窗页继续监听

模板（visit_assets/）：
  zg_chat_btn.png  聊天入口按钮（地图右下角，白色气泡图标）
  zg_joined.png    总攻页底部「已参与」按钮（用于底部条匹配）
  zg_back.png      总攻页左下角返回按钮
  zg_done.png      总攻完成弹框标题（判断弹框出现）
  zg_ok.png        弹框「确定」按钮
  （zg_card / zg_expired / zg_applied 不再使用：聊天卡片模板已失效，
   现在靠颜色判据扫绿按钮，灰色按钮天然不可参与）
"""
import threading
import time
import cv2
import numpy as np
from pathlib import Path

ASSETS = Path(__file__).parent / "visit_assets"

# 多尺度匹配：模板来自窗口截图(约816px游戏宽)，实际模拟器 1080~1092px
SCALES = [1.0, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30, 1.35, 1.40, 1.50, 1.60,
          0.95, 0.90, 0.85, 0.80]

# 总攻页按钮扫描参数（按钮都在页面右侧 0.58~0.95w 这一竖条上）
BTN_X1, BTN_X2 = 0.58, 0.95
BTN_MIN_ROW_PIX = 40    # 一行里命中像素数下限
BTN_MIN_BAND_H = 0.015  # 按钮带最小高度（占屏高比例），滤细噪声

# 列表页翻页参数
LIST_MAX_CLICKS = 40    # 单轮列表页最多点击参与次数（防死循环）
LIST_MAX_SWIPES = 12    # 单轮列表页最多上滑次数
SWIPE_FP_DIFF = 1.5     # 滑动前后指纹差 < 此值 = 画面没动 = 到底


def _load(name):
    img = cv2.imread(str(ASSETS / f"{name}.png"), cv2.IMREAD_COLOR)
    return img


def _match(frame, tpl, region=None, scales=SCALES):
    """多尺度模板匹配。返回 (score, x, y, w, h)，未给tpl返回(-1,0,0,0,0)。
    region: (x1,y1,x2,y2) 相对整图的搜索区域（像素），None=全图。"""
    if tpl is None:
        return -1.0, 0, 0, 0, 0
    fx1, fy1, fx2, fy2 = region or (0, 0, frame.shape[1], frame.shape[0])
    sub = frame[max(0, fy1):min(frame.shape[0], fy2),
                max(0, fx1):min(frame.shape[1], fx2)]
    th, tw = tpl.shape[:2]
    best = (-1.0, 0, 0, 0, 0)
    for s in scales:
        nw, nh = int(tw * s), int(th * s)
        if nw < 8 or nh < 8 or nh > sub.shape[0] or nw > sub.shape[1]:
            continue
        t = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_AREA)
        g1 = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(g1, g2, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score > best[0]:
            best = (score, loc[0] + fx1, loc[1] + fy1, nw, nh)
    return best


def _fingerprint(frame):
    """整屏指纹（缩到48x84灰度），用于列表页滑动前后判断画面是否移动。"""
    g = cv2.cvtColor(cv2.resize(frame, (48, 84), interpolation=cv2.INTER_AREA),
                     cv2.COLOR_BGR2GRAY)
    return g.astype(np.float32)


class ZongGongMonitor:
    """总攻自动参加。engines_ref: 返回 {name: VisitEngine} 的可调用（引用server的字典）。"""

    def __init__(self, engines_ref, on_log=None, poll_interval=3.0):
        self._engines_ref = engines_ref
        self.on_log = on_log or (lambda s: None)
        self.poll_interval = poll_interval        # 轮询间隔（秒）
        self._running = False
        self._thread = None
        self._instances = []          # 要监控的实例名
        self._cooldown = {}           # name -> 上次参加完成时间（短暂冷却防UI竞态）
        self._miss = {}               # name -> 连续不在地图/聊天页的轮数（自愈用）
        self._last_record = {}        # 卡死/错误记录节流：key -> 上次记录时间
        self.COOLDOWN_SEC = 5         # 参加一轮后短暂冷却（按钮变灰动画期间不重扫）

        self.tpl_chat = _load("zg_chat_btn")
        self.tpl_joined = _load("zg_joined")
        self.tpl_back = _load("zg_back")
        self.tpl_done = _load("zg_done")
        self.tpl_ok = _load("zg_ok")

    # ── 状态 ──
    @property
    def running(self):
        return self._running

    def status(self):
        return {
            "running": self._running,
            "instances": list(self._instances),
            "templates": {n: (v is not None) for n, v in
                          [("chat_btn", self.tpl_chat), ("joined", self.tpl_joined),
                           ("back", self.tpl_back), ("done", self.tpl_done),
                           ("ok", self.tpl_ok)]},
        }

    def start(self, instances=None):
        if self._running:
            return False
        # 旧线程可能还在收尾（一次 _check_one 流程最长十几秒）。
        # 必须等它真正退出再开新线程，否则两个线程会同时操作
        # 同一台模拟器互相干扰（实测：一个刚点参与、另一个就关聊天）。
        if self._thread is not None and self._thread.is_alive():
            self.on_log("[总攻] 等待上一轮监控线程退出...")
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                self.on_log("[总攻] 旧线程仍未退出，放弃本次启动")
                return False
        engines = self._engines_ref()
        names = list(instances) if instances else list(engines.keys())
        self._instances = names
        missing = [n for n, t in [("zg_chat_btn", self.tpl_chat),
                                  ("zg_joined", self.tpl_joined),
                                  ("zg_back", self.tpl_back),
                                  ("zg_ok", self.tpl_ok)] if t is None]
        if missing:
            self.on_log(f"[总攻] 缺少模板图片: {','.join(missing)}，无法启动")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.on_log(f"[总攻] 监控已启动，实例: {', '.join(names) or '无'}")
        return True

    def stop(self):
        self._running = False
        # 不在这里 join：会卡住面板。旧线程很快会自己退出，
        # start() 里会先等它退出再开新线程，保证不会双线程并行。
        self.on_log("[总攻] 监控已停止")

    # ── 卡死/错误持久化记录（用户要求）：追加写 zg_errors.log，重启不丢 ──
    def _record(self, name, msg):
        try:
            key = f"{name}:{msg}"
            now = time.time()
            if now - self._last_record.get(key, 0) < 60:   # 同类事件60秒内只记一次，防刷屏
                return
            self._last_record[key] = now
            ts = time.strftime("%m-%d %H:%M:%S")
            log = Path(__file__).parent / "zg_errors.log"
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{name}] {msg}\n")
        except Exception:
            pass

    # ── 主循环 ──
    def _loop(self):
        while self._running:
            engines = self._engines_ref()
            for name in list(self._instances):
                if not self._running:
                    break
                engine = engines.get(name)
                if engine is None:
                    continue
                try:
                    self._check_one(name, engine)
                except Exception as e:
                    self.on_log(f"[总攻][{name}] 轮询异常: {e}")
                    self._record(name, f"轮询异常: {e}")
                time.sleep(0.5)   # 实例间隔
            time.sleep(self.poll_interval)

    # ── 单轮检测 ──
    def _check_one(self, name, engine):
        now = time.time()
        if now - self._cooldown.get(name, 0) < self.COOLDOWN_SEC:
            return
        frame = engine.screenshot()

        # 1. 先清「总攻完成!」弹框（可能有多个，循环处理完才继续）
        frame = self._dismiss_all_popups(name, engine, frame)
        h, w = frame.shape[:2]

        # 2. 页面判定：聊天页优先（驻留态），其次地图页，最后卡死处理
        on_chat = self._is_chat_page(frame)
        if not on_chat:
            rs, bx, by, bw, bh = _match(
                frame, self.tpl_chat, region=(int(w * 0.76), int(h * 0.68), w, int(h * 0.88)))
            if rs >= 0.62:
                # 在总攻页（活动地图）→ 点进聊天窗页
                self._miss[name] = 0
                self.on_log(f"[总攻][{name}] 从总攻页点进聊天窗")
                engine.tap(bx + bw // 2, by + bh // 2)
                time.sleep(1.6)
                frame = engine.screenshot()
                on_chat = self._is_chat_page(frame)
        else:
            self._miss[name] = 0

        if not on_chat:
            # 不在聊天页也不在地图页：可能停在总攻详情/列表页（上轮返回没生效）
            h, w = frame.shape[:2]
            bs, bxx, byy, bww, bhh = _match(frame, self.tpl_back,
                                            region=(0, int(h * 0.85), int(w * 0.25), h))
            if bs >= 0.60:
                engine.tap(bxx + bww // 2, byy + bhh // 2)
                self._miss[name] = 0
                self._record(name, "卡死：检测到停在总攻页，已按返回恢复")
                self.on_log(f"[总攻][{name}] 检测到停在总攻页，按返回恢复（已记录）")
                time.sleep(1.2)
                return
            # 列表页（无返回键）：检测底部✕按实际位置关闭，不等60秒自愈
            cx = self._find_close_x(frame)
            if cx:
                engine.tap(cx[0], cx[1])
                self._miss[name] = 0
                self._record(name, f"卡死：检测到停在带✕的页面({cx[0]},{cx[1]})，已点击关闭")
                self.on_log(f"[总攻][{name}] 检测到底部✕({cx[0]},{cx[1]})，点击关闭（已记录）")
                time.sleep(1.2)
                return
            self._miss[name] = self._miss.get(name, 0) + 1
            if self._miss[name] >= 20:   # 连续~60秒不在已知页面
                self._miss[name] = 0
                self._record(name, "异常：连续60秒不在活动地图，触发自愈")
                self.on_log(f"[总攻][{name}] 连续60秒不在活动地图，尝试自愈返回（已记录）")
                self._heal_to_map(engine, name, w, h)
            return

        # 3. 聊天窗页：扫描绿色「参与」按钮条（最底部=最新优先）
        h, w = frame.shape[:2]
        btns = self._scan_chat_join_buttons(frame)
        if not btns:
            return   # 没有可参与的，驻留等待（聊天页自动刷新新消息）

        yc, xc = btns[0]
        self.on_log(f"[总攻][{name}] 点击最新绿色参与条 y={yc:.3f}")
        engine.tap(int(xc * w), int(yc * h))

        # 轮询等待离开聊天页（连续2帧既不是聊天页也不是地图页 = 已进入总攻页）
        if not self._wait_leave_chat(engine):
            self.on_log(f"[总攻][{name}] 点击参与条后未离开聊天页，下轮重试")
            return
        time.sleep(0.8)   # 等页面渲染完再判定按钮颜色

        # 4. 处理总攻页（详情页/列表页）
        self._handle_zg_page(name, engine)

        # 5. 点返回回到总攻页（自适应：落在聊天页就点X）
        self._return_to_map(name, engine)

        # 6. 回到总攻页后再清一轮弹框（可能有多个）→ 重新点进聊天窗页
        frame = engine.screenshot()
        frame = self._dismiss_all_popups(name, engine, frame)
        if not self._is_chat_page(frame):
            h, w = frame.shape[:2]
            rs, bx, by, bw, bh = _match(
                frame, self.tpl_chat, region=(int(w * 0.76), int(h * 0.68), w, int(h * 0.88)))
            if rs >= 0.62:
                engine.tap(bx + bw // 2, by + bh // 2)
                time.sleep(1.6)
                self.on_log(f"[总攻][{name}] 已回到聊天窗页继续监听")
            else:
                self.on_log(f"[总攻][{name}] 未找到聊天入口，下轮重试")
        else:
            self.on_log(f"[总攻][{name}] 返回后已在聊天窗页，继续监听")

        self._cooldown[name] = time.time()
        self.on_log(f"[总攻][{name}] 本轮流程完成")

    # ── 「总攻完成!」弹框：循环清空（每次回总攻页可能有多个排队弹框） ──
    def _dismiss_all_popups(self, name, engine, frame, max_popups=6):
        """连续检测并点掉「总攻完成!」弹框，直到没有为止。返回最新截图。"""
        if self.tpl_done is None:
            return frame
        for _ in range(max_popups):
            h, w = frame.shape[:2]
            score, _, _, _, _ = _match(frame, self.tpl_done,
                                       region=(int(w * 0.1), 0, int(w * 0.9), int(h * 0.4)))
            if score < 0.68:
                return frame
            self.on_log(f"[总攻][{name}] 检测到总攻完成弹框(score={score:.2f})，点击确定")
            if self.tpl_ok is not None:
                oscore, ox, oy, ow, oh = _match(frame, self.tpl_ok,
                                                region=(int(w * 0.15), int(h * 0.75),
                                                        int(w * 0.85), h))
                if oscore >= 0.60:
                    engine.tap(ox + ow // 2, oy + oh // 2)
                else:
                    engine.tap(int(w * 0.5), int(h * 0.845))
            else:
                engine.tap(int(w * 0.5), int(h * 0.845))
            time.sleep(1.2)
            frame = engine.screenshot()
        self._record(name, "异常：连续多个总攻完成弹框未清完")
        return frame

    # ── 聊天页绿按钮条扫描 ──
    def _scan_chat_join_buttons(self, frame):
        """扫描聊天页中的绿色「参与」按钮条（聊天卡片正下方）。

        返回 [(y中心比例, x中心比例), ...]，按y降序（最底部=最新优先）。
        实测颜色：绿参与 BGR≈(53,216,149)，横贯约0.5w；灰「已申请/申请中/
        已过期」≈(160,160,160) 不满足绿色判据，天然不会命中。
        扫描范围 y 0.08~0.84h（避开底部绿色发送按钮）、x 0.25~0.95w；
        每行要求绿像素 ≥ 扫描宽25%（地块小缩略图/小图标不会命中），
        按钮带高度 ≥ 0.02h。
        """
        h, w = frame.shape[:2]
        y1, y2 = int(h * 0.08), int(h * 0.84)
        x1, x2 = int(w * 0.25), int(w * 0.95)
        sub = frame[y1:y2, x1:x2].astype(int)
        b, g, r = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
        green = (g > 150) & (g - r > 60) & (g - b > 60)
        rows = green.sum(axis=1)
        hits = np.where(rows > (x2 - x1) * 0.25)[0]
        out = []
        bands = []
        for y in hits:
            if bands and y - bands[-1][1] <= 10:
                bands[-1][1] = y
            else:
                bands.append([y, y])
        for y0, ye in bands:
            if (ye - y0) < h * 0.02:
                continue
            seg = green[y0:ye + 1]
            xs = np.where(seg.any(axis=0))[0]
            yc = (y0 + ye) / 2 + y1
            xc = (xs.min() + xs.max()) / 2 + x1
            out.append((yc / h, xc / w))
        return sorted(out, reverse=True)

    # ── 等待离开聊天页 ──
    def _wait_leave_chat(self, engine, timeout=4.0):
        """点绿条后轮询确认已离开聊天页（连续2帧非聊天/地图页=进入了总攻页）。"""
        consec = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.3)
            frame = engine.screenshot()
            if self._is_chat_page(frame):
                consec = 0
                continue
            h, w = frame.shape[:2]
            rs, *_ = _match(frame, self.tpl_chat,
                            region=(int(w * 0.76), int(h * 0.68), w, int(h * 0.88)))
            if rs >= 0.62:
                consec = 0
                continue
            consec += 1
            if consec >= 2:
                return True
        return False

    # ── 总攻页处理：详情页 / 列表页 ──
    def _handle_zg_page(self, name, engine):
        """总攻页有两种形态（点聊天绿条后进哪个，取决于该地块有几条总攻）：
        a) 详情页（单条总攻）：底部一根横贯的大按钮（绿=未参与→点它）
        b) 列表页（同地块多条）：底部纯紫无按钮，右侧每条各带绿/灰按钮→子循环逐个点
        用底部条(0.88h~1h, 0.3w~0.9w)的颜色占比区分——绝不扫上半屏，
        详情页上半是实时地图（绿虚线/杀敌toast会误报，已实测踩坑）。
        绿>0.25=详情页未参与；灰>0.25=详情页已参与；两者都低=列表页。
        """
        frame = engine.screenshot()
        fh, fw = frame.shape[:2]
        sub = frame[int(0.88 * fh):fh, int(0.30 * fw):int(0.90 * fw)].astype(int)
        b_, g_, r_ = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
        green_ratio = ((g_ > 160) & (g_ - r_ > 40) & (g_ - b_ > 80)).mean()
        mx = np.maximum(np.maximum(b_, g_), r_)
        mn = np.minimum(np.minimum(b_, g_), r_)
        gray_ratio = ((mx - mn < 25) & (mn > 100) & (mx < 210)).mean()
        if green_ratio > 0.25:
            engine.tap(int(fw * 0.58), int(fh * 0.934))   # 详情页底部大按钮，原坐标
            self.on_log(f"[总攻][{name}] 详情页：点击底部参与按钮")
        elif gray_ratio > 0.25:
            self.on_log(f"[总攻][{name}] 详情页：底部按钮已灰（已参与），跳过")
        else:
            self._participate_list(name, engine)

    def _find_close_x(self, frame):
        """检测底部中央的白色✕关闭按钮（列表页专用），返回中心像素坐标或None。

        列表页没有左下角返回键，只有底部 ✕，且其中心在 ~0.968h——
        写死 0.955h 会差 25px 点空（11-02 踩过的坑），必须检测后按实际位置点。
        搜索区域 x 0.30~0.70w、y 0.88h~底部，白色像素（RGB全>225）质心。
        注意：本方法只在"已确认不在地图页"时调用——地图底部中央的 ✕ 是
        「退出活动」，绝不能误点。
        """
        h, w = frame.shape[:2]
        x1, x2 = int(w * 0.30), int(w * 0.70)
        y1, y2 = int(h * 0.88), h
        sub = frame[y1:y2, x1:x2]
        white = (sub[:, :, 0] > 225) & (sub[:, :, 1] > 225) & (sub[:, :, 2] > 225)
        if int(white.sum()) < 80:   # ✕ 本体像素量级几百，80 以下视为没有
            return None
        ys, xs = np.where(white)
        cx = int(np.median(xs)) + x1
        cy = int(np.median(ys)) + y1
        return cx, cy

    def _bands_same(self, b1, b2, tol=0.02):
        """比较两次扫描的按钮带列表是否一致（数量、位置、颜色都同）。"""
        if len(b1) != len(b2):
            return False
        for (a1, b_1, act1), (a2, b_2, act2) in zip(b1, b2):
            if act1 != act2 or abs(a1 - a2) > tol or abs(b_1 - b_2) > tol:
                return False
        return True

    def _participate_list(self, name, engine):
        """列表页参与子循环：扫描→逐个点绿→上滑翻页→重扫，直到到底。

        - 绿「参与」→ 点；灰「已申请/申请中/已过期」→ 天然跳过
        - 点击没生效的按钮，重扫时会补点（点完 continue 重扫本屏）
        - 本屏无绿 → 上滑一屏；滑动后画面指纹没动 或 按钮带与滑动前一致 = 到底
          （不能只看指纹：列表页背景有星光/呼吸动画，倒计时每秒都在变，
           指纹差可能一直大于阈值，导致白白滑满 12 次）
        - 上限 LIST_MAX_CLICKS / LIST_MAX_SWIPES 防死循环
        """
        clicks = 0
        swipes = 0
        while swipes < LIST_MAX_SWIPES and clicks < LIST_MAX_CLICKS:
            frame = engine.screenshot()
            fh, fw = frame.shape[:2]
            bands = self._scan_join_buttons(frame)
            greens = [(a, bnd) for a, bnd, act in bands if act]
            if greens:
                for a, bnd in greens:
                    if clicks >= LIST_MAX_CLICKS:
                        break
                    engine.tap(int(fw * 0.78), int((a + bnd) / 2 * fh))
                    clicks += 1
                    self.on_log(f"[总攻][{name}] 列表页：点击参与按钮 "
                                f"y={(a + bnd) / 2:.3f}（第{clicks}次）")
                    time.sleep(0.9)
                continue   # 重扫本屏：没点中的会补点，全灰了就走翻页
            # 本屏没有绿按钮 → 上滑一屏
            fp_before = _fingerprint(frame)
            engine.swipe(int(fw * 0.5), int(fh * 0.75),
                         int(fw * 0.5), int(fh * 0.30), duration_ms=450)
            time.sleep(0.8)
            frame = engine.screenshot()
            fp_after = _fingerprint(frame)
            diff = float(np.abs(fp_after - fp_before).mean())
            bands_after = self._scan_join_buttons(frame)
            greens_after = [(a, bnd) for a, bnd, act in bands_after if act]
            if greens_after:
                continue   # 新屏有绿按钮，回到循环开头去点
            if diff < SWIPE_FP_DIFF or self._bands_same(bands, bands_after):
                self.on_log(f"[总攻][{name}] 列表页已到底(fp_diff={diff:.1f})，"
                            f"共参与{clicks}条（{swipes}次翻页）")
                return
            swipes += 1
        self._record(name, f"列表页达到上限：clicks={clicks}, swipes={swipes}")
        self.on_log(f"[总攻][{name}] 列表页达到操作上限，共参与{clicks}条（已记录）")

    def _scan_join_buttons(self, frame):
        """扫描总攻页右侧按钮列，返回 [(y0比例, y1比例, active), ...]（按y升序）。

        active=True  → 绿色「参与」按钮（亮绿 BGR≈(60,204,132)）
        active=False → 灰色「已申请/申请中/已过期」按钮（中性灰 ≈(144,144,144)）
        用颜色通道关系区分，右侧绿色徽章(BGR≈(107,99,114)，G不占优)、
        粉/紫色卡片底、页面紫背景的 G 通道都很低，不会误报。
        每行还要求命中像素数足够、按钮带高度>=BTN_MIN_BAND_H，滤掉细噪声。
        """
        h, w = frame.shape[:2]
        x1, x2 = int(w * BTN_X1), int(w * BTN_X2)
        sub = frame[:, x1:x2].astype(int)
        b, g, r = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
        green = (g > 160) & (g - r > 40) & (g - b > 80)
        mx = np.maximum(np.maximum(b, g), r)
        mn = np.minimum(np.minimum(b, g), r)
        gray = (mx - mn < 25) & (mn > 100) & (mx < 210)
        out = []
        for mask, active in ((green, True), (gray, False)):
            rows = mask.sum(axis=1)
            hits = np.where(rows > BTN_MIN_ROW_PIX)[0]
            bands = []
            for y in hits:
                if bands and y - bands[-1][1] <= 8:   # 相邻8px内并入同一条带
                    bands[-1][1] = y
                else:
                    bands.append([y, y])
            for y0, y1 in bands:
                if (y1 - y0) / h < BTN_MIN_BAND_H:
                    continue
                out.append((y0 / h, y1 / h, active))
        return sorted(out, key=lambda t: t[0])

    # ── 返回总攻页（自适应） ──
    def _return_to_map(self, name, engine):
        """点左下角返回回到总攻页。每轮"截图判断→点击"，最多5轮。

        每轮都要重新检测「总攻完成!」弹框：参加后弹框可能在任何时刻弹出来
        盖住全屏，只按返回/点X会全部落在暗色遮罩上（11-06 实测卡死）。
        正常路径：返回键 → 直接回总攻页。
        自适应：列表页没有返回键 → 检测白色✕按实际位置点；
                若落在聊天页（旧版行为）→ 点底部X。
        """
        returned = False
        for attempt in range(5):
            frame = engine.screenshot()
            h, w = frame.shape[:2]
            # 0. 弹框优先：盖住全屏时返回键/X 都点不到，必须先点确定
            if self.tpl_done is not None:
                ps, *_ = _match(frame, self.tpl_done,
                                region=(int(w * 0.1), 0, int(w * 0.9), int(h * 0.4)))
                if ps >= 0.68:
                    self.on_log(f"[总攻][{name}] 返回途中弹出总攻完成弹框，先点确定")
                    if self.tpl_ok is not None:
                        oscore, ox, oy, ow, oh = _match(frame, self.tpl_ok,
                                                        region=(int(w * 0.15), int(h * 0.75),
                                                                int(w * 0.85), h))
                        if oscore >= 0.60:
                            engine.tap(ox + ow // 2, oy + oh // 2)
                        else:
                            engine.tap(int(w * 0.5), int(h * 0.845))
                    else:
                        engine.tap(int(w * 0.5), int(h * 0.845))
                    time.sleep(1.2)
                    continue
            # 已在总攻页（右下角聊天入口可见，且不是聊天窗页）→ 完成
            rs, *_ = _match(frame, self.tpl_chat,
                            region=(int(w * 0.76), int(h * 0.68), w, int(h * 0.88)))
            if rs >= 0.62 and not self._is_chat_page(frame):
                returned = True
                if attempt:
                    self.on_log(f"[总攻][{name}] 已确认返回总攻页（操作了{attempt}次）")
                break
            # 返回键可见 → 在总攻详情页，按返回
            bs, bxx, byy, bww, bhh = _match(frame, self.tpl_back,
                                            region=(0, int(h * 0.85), int(w * 0.25), h))
            if bs >= 0.60:
                engine.tap(bxx + bww // 2, byy + bhh // 2)
                self.on_log(f"[总攻][{name}] 按返回（第{attempt + 1}次）")
            elif self._is_chat_page(frame):
                engine.tap(int(w * 0.5), int(h * 0.955))
                self.on_log(f"[总攻][{name}] 落在聊天窗页，点X关闭（第{attempt + 1}次）")
            else:
                # 列表页等无返回键页面：检测白色✕按实际位置点（✕中心~0.968h，
                # 写死0.955h会点空——11-02踩坑，勿回退）
                cx = self._find_close_x(frame)
                if cx:
                    engine.tap(cx[0], cx[1])
                    self.on_log(f"[总攻][{name}] 检测到底部✕({cx[0]},{cx[1]})，"
                                f"点击关闭（第{attempt + 1}次）")
                else:
                    engine.tap(int(w * 0.5), int(h * 0.968))
                    self.on_log(f"[总攻][{name}] 未识别到✕，按默认位置点底部中央"
                                f"（第{attempt + 1}次）")
            time.sleep(1.2)
        if not returned:
            # 卡死现场存图，方便事后定位
            try:
                stuck_dir = Path(__file__).parent / "debug_zg_stuck"
                stuck_dir.mkdir(exist_ok=True)
                cv2.imwrite(str(stuck_dir / f"stuck_{time.strftime('%m%d_%H%M%S')}.jpg"),
                            frame)
            except Exception:
                pass
            self._record(name, "卡死：4轮返回后仍未回到总攻页（已存现场截图）")
            self.on_log(f"[总攻][{name}] 卡死：4轮后仍未回总攻页（已记录+存图）")

    def _is_chat_page(self, frame):
        """判断当前是否在聊天窗页。

        聊天页独有左侧深色频道栏（x 0.02~0.16）。实测深色像素占比：
        聊天页 0.57，列表页 0.14，详情页 0.20，地图 0.34——阈值 0.45 干净分离。
        """
        h, w = frame.shape[:2]
        sub = frame[int(0.10 * h):int(0.85 * h), int(0.02 * w):int(0.16 * w)]
        dark = (sub.max(axis=2) < 90).mean()
        return dark >= 0.45

    def _heal_to_map(self, engine, name, w, h):
        """自愈：不在活动地图时尝试返回。

        优先方案：visit_assets/zg_home_entry.png（游戏主页上「夺金」入口按钮模板，
        有的话模板匹配点击，最可靠）。
        兜底方案：地图底部中央的X和主页入口常在同一位置，点(0.5w, 0.965h)尝试切换。
        点完等2秒重新截图确认，没成功下一轮再试。
        """
        frame = engine.screenshot()
        entry = _load("zg_home_entry")
        if entry is not None:
            es, ex, ey, ew, eh = _match(frame, entry)
            if es >= 0.65:
                engine.tap(ex + ew // 2, ey + eh // 2)
                self.on_log(f"[总攻][{name}] 自愈：点击夺金入口(score={es:.2f})")
                time.sleep(2.0)
                return
            self.on_log(f"[总攻][{name}] 自愈：主页未找到夺金入口模板")
        # 兜底：直接点底部中央位置（主页/地图的切换按钮常在同一处）
        engine.tap(int(w * 0.5), int(h * 0.965))
        self.on_log(f"[总攻][{name}] 自愈：点击底部中央按钮尝试返回活动")
        time.sleep(2.0)
        # 验证
        frame = engine.screenshot()
        fh, fw = frame.shape[:2]
        rs, _, _, _, _ = _match(frame, self.tpl_chat,
                                region=(int(fw * 0.76), int(fh * 0.68),
                                        fw, int(fh * 0.88)))
        self.on_log(f"[总攻][{name}] 自愈后{'已回到' if rs >= 0.62 else '尚未回到'}活动地图")
