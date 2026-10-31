# -*- coding: utf-8 -*-
"""
总攻自动参加监控（海岛夺金活动）——红点即时 + 定时兜底 双保险版

流程（每个实例独立执行）：
  1. 每~3秒截图：先检查「总攻完成」弹框（有就点确定）
  2. 找右下角聊天入口按钮 → 按钮右上角检测红色气泡：
     - 有红点 → 立即点开聊天页（即时响应）
     - 无红点但距上次检查超过60秒 → 也点开聊天页（兜底防漏）
     - 都不满足 → 继续等待
  3. 在聊天页找所有「我发起了总攻!」卡片 → 点最底部（最新）一张
  4. 进入总攻页 → 检测底部按钮：若已是「已参与」则跳过，否则点按钮参加
  5. 点左下角返回按钮 → 回到聊天页 → 点底部 X 关闭聊天

模板（visit_assets/）：
  zg_chat_btn.png  聊天入口按钮（地图右下角，白色气泡图标）
  zg_card.png      聊天里的总攻卡片（紫色部分，稳定区域）
  zg_joined.png    总攻页底部「已参与」按钮
  zg_back.png      总攻页左下角返回按钮
  zg_done.png      总攻完成弹框标题（判断弹框出现）
  zg_ok.png        弹框「确定」按钮
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

# 红色气泡判定（按钮右上角小区域内亮红像素数）
RED_MIN_PIXELS = 25


def _load(name):
    img = cv2.imread(str(ASSETS / f"{name}.png"), cv2.IMREAD_COLOR)
    return img


def _match_all(frame, tpl, region=None, scales=SCALES, thresh=0.65):
    """多尺度模板匹配，返回所有得分>=thresh的位置，按y升序。
    每个元素 (score, x, y, w, h)。同尺度内做简单去重（IoU>0.5保留高分）。"""
    if tpl is None:
        return []
    fx1, fy1, fx2, fy2 = region or (0, 0, frame.shape[1], frame.shape[0])
    sub = frame[max(0, fy1):min(frame.shape[0], fy2),
                max(0, fx1):min(frame.shape[1], fx2)]
    th, tw = tpl.shape[:2]
    hits = []
    for s in scales:
        nw, nh = int(tw * s), int(th * s)
        if nw < 8 or nh < 8 or nh > sub.shape[0] or nw > sub.shape[1]:
            continue
        t = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_AREA)
        g1 = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(g1, g2, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= thresh)
        for x, y in zip(xs, ys):
            hits.append((float(res[y, x]), x + fx1, y + fy1, nw, nh))
    if not hits:
        return []
    # 按得分降序去重
    hits.sort(key=lambda h: -h[0])
    kept = []
    for h in hits:
        _, hx, hy, hw, hh = h
        dup = False
        for k in kept:
            _, kx, ky, kw, kh = k
            # 中心距离小于尺寸一半视为同一目标
            if abs(hx - kx) < hw * 0.5 and abs(hy - ky) < hh * 0.5:
                dup = True
                break
        if not dup:
            kept.append(h)
    return sorted(kept, key=lambda h: h[2])   # 按y升序：y最大=最下面=最新


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


def _count_bright_red(frame, x1, y1, x2, y2):
    """区域内亮红色像素数（红气泡徽标）"""
    h, w = frame.shape[:2]
    sub = frame[max(0, int(y1)):min(h, int(y2)), max(0, int(x1)):min(w, int(x2))]
    if sub.size == 0:
        return 0
    b, g, r = sub[:, :, 0].astype(int), sub[:, :, 1].astype(int), sub[:, :, 2].astype(int)
    mask = (r > 165) & (g < 95) & (b < 95)
    return int(mask.sum())


class ZongGongMonitor:
    """总攻自动参加。engines_ref: 返回 {name: VisitEngine} 的可调用（引用server的字典）。"""

    def __init__(self, engines_ref, on_log=None, poll_interval=3.0, check_interval=60.0):
        self._engines_ref = engines_ref
        self.on_log = on_log or (lambda s: None)
        self.poll_interval = poll_interval        # 红点检测间隔（秒）
        self.check_interval = check_interval      # 无红点时的兜底检查间隔（秒）
        self._running = False
        self._thread = None
        self._instances = []          # 要监控的实例名
        self._cooldown = {}           # name -> 上次参加完成时间（同一波总攻不重复点）
        self._last_check = {}         # name -> 上次点开聊天框检查的时间
        self._miss = {}               # name -> 连续不在活动地图的轮数（自愈用）
        self.COOLDOWN_SEC = 60        # 参加一次后60秒内不再重复进（避免反复开关聊天）

        self.tpl_chat = _load("zg_chat_btn")
        self.tpl_card = _load("zg_card")
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
                          [("chat_btn", self.tpl_chat), ("card", self.tpl_card),
                           ("joined", self.tpl_joined), ("back", self.tpl_back)]},
        }

    def start(self, instances=None):
        if self._running:
            return False
        engines = self._engines_ref()
        names = list(instances) if instances else list(engines.keys())
        self._instances = names
        missing = [n for n, t in [("zg_chat_btn", self.tpl_chat),
                                  ("zg_card", self.tpl_card),
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
        self.on_log("[总攻] 监控已停止")

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
                time.sleep(0.5)   # 实例间隔
            time.sleep(self.poll_interval)

    def _check_one(self, name, engine):
        now = time.time()
        frame = engine.screenshot()
        h, w = frame.shape[:2]

        # 0. 先检查「总攻完成」弹框：出现就点确定关掉
        if self._dismiss_done_popup(name, engine, frame):
            frame = engine.screenshot()
            h, w = frame.shape[:2]

        if now - self._cooldown.get(name, 0) < self.COOLDOWN_SEC:
            return

        # 1. 找聊天入口按钮（右下角），判断是否进入聊天页
        rx1, ry1 = int(w * 0.76), int(h * 0.68)
        score, bx, by, bw, bh = _match(
            frame, self.tpl_chat, region=(rx1, ry1, w, int(h * 0.88)))
        if score < 0.62:
            # 不在活动地图（被退到游戏主页/其他界面）→ 自愈
            self._miss[name] = self._miss.get(name, 0) + 1
            if self._miss[name] >= 20:   # 连续~60秒不在地图
                self._miss[name] = 0
                self.on_log(f"[总攻][{name}] 连续60秒不在活动地图，尝试自愈返回")
                self._heal_to_map(engine, name, w, h)
            return
        self._miss[name] = 0

        # 2. 红点即时 + 60秒兜底，二者满足其一才点开聊天框
        red = _count_bright_red(frame, bx + bw * 0.55, by - bh * 0.18,
                                bx + bw * 1.12, by + bh * 0.35)
        has_red = red >= RED_MIN_PIXELS
        due = now - self._last_check.get(name, 0) >= self.check_interval
        if not has_red and not due:
            return
        if has_red:
            self.on_log(f"[总攻][{name}] 发现聊天红点(red={red})，进入聊天页")
        else:
            self.on_log(f"[总攻][{name}] 定时兜底检查（{self.check_interval:.0f}秒），进入聊天页")
        self._last_check[name] = now

        engine.tap(bx + bw // 2, by + bh // 2)
        time.sleep(1.6)

        frame = engine.screenshot()
        h, w = frame.shape[:2]

        # 3. 找总攻卡片——从最底部（最新）往上依次尝试，直到真正进入总攻页
        cards = _match_all(frame, self.tpl_card,
                           region=(int(w * 0.15), 0, w, int(h * 0.95)), thresh=0.65)
        if not cards:
            self._close_chat(engine, w, h)   # 没有总攻，关聊天继续
            return

        # 从最底部（最新）开始尝试，最多试 3 张
        for idx, (score, cx, cy, cw2, ch2) in enumerate(reversed(cards[-3:])):
            tag = "最新一张" if idx == 0 else f"倒数第{idx + 1}张"
            self.on_log(f"[总攻][{name}] 找到{len(cards)}张卡片，点击{tag}(score={score:.2f})")
            engine.tap(cx + cw2 // 2, cy + int(ch2 * 0.8))
            time.sleep(1.8)

            frame = engine.screenshot()
            h, w = frame.shape[:2]
            if self._on_zonggong_page(frame):
                break
            self.on_log(f"[总攻][{name}] {tag}未进入总攻页，尝试下一张")
            # 可能还在聊天页，回到聊天继续点下一张
            self._close_chat(engine, w, h)
            time.sleep(0.8)
            frame = engine.screenshot()
            h, w = frame.shape[:2]
            if not _match_all(frame, self.tpl_card, thresh=0.65):
                self.on_log(f"[总攻][{name}] 已离开聊天页，结束本轮")
                return
        else:
            self.on_log(f"[总攻][{name}] 所有卡片都未进入总攻页，关闭聊天")
            self._close_chat(engine, w, h)
            self._cooldown[name] = now
            return

        # 5. 直接点底部按钮框（不做任何判断）：
        #    未参与→点击申请参加；已参与→灰按钮点了没副作用。
        #    坐标取自总攻页底部按钮中心（避开左下返回键）。
        engine.tap(int(w * 0.58), int(h * 0.934))
        self.on_log(f"[总攻][{name}] 已点击总攻按钮框")
        time.sleep(1.5)
        # 点完再确认一次状态，仅记录日志
        frame2 = engine.screenshot()
        fh, fw = frame2.shape[:2]
        j2, *_ = _match(frame2, self.tpl_joined,
                        region=(int(fw * 0.12), int(fh * 0.85), int(fw * 0.99), fh))
        self.on_log(f"[总攻][{name}] 点击后按钮状态: 已参与(score={j2:.2f})")

        # 6. 点左下角返回
        frame = engine.screenshot()
        h, w = frame.shape[:2]
        bs, bxx, byy, bww, bhh = _match(frame, self.tpl_back,
                                        region=(0, int(h * 0.85), int(w * 0.25), h))
        if bs >= 0.60:
            engine.tap(bxx + bww // 2, byy + bhh // 2)
        else:
            engine.tap(int(w * 0.075), int(h * 0.938))
        time.sleep(1.2)

        # 7. 关闭聊天页（底部中央 X）
        self._close_chat(engine, w, h)
        self._cooldown[name] = time.time()
        self.on_log(f"[总攻][{name}] 本轮流程完成，继续监听")

    def _dismiss_done_popup(self, name, engine, frame):
        """检测「总攻完成!」弹框，出现则点确定。返回是否处理过。"""
        h, w = frame.shape[:2]
        if self.tpl_done is None:
            return False
        score, _, _, _, _ = _match(frame, self.tpl_done,
                                   region=(int(w * 0.1), 0, int(w * 0.9), int(h * 0.4)))
        if score < 0.68:
            return False
        self.on_log(f"[总攻][{name}] 检测到总攻完成弹框(score={score:.2f})，点击确定")
        if self.tpl_ok is not None:
            oscore, ox, oy, ow, oh = _match(frame, self.tpl_ok,
                                            region=(int(w * 0.15), int(h * 0.75), int(w * 0.85), h))
            if oscore >= 0.60:
                engine.tap(ox + ow // 2, oy + oh // 2)
            else:
                engine.tap(int(w * 0.5), int(h * 0.845))
        else:
            engine.tap(int(w * 0.5), int(h * 0.845))
        time.sleep(1.2)
        return True

    def _on_zonggong_page(self, frame):
        """判断当前是否在总攻页。

        总攻页特征：底部有一条很宽的横条按钮（参加/已参与），
        左下角有圆形返回按钮。这里用两个判据：
          1) 聊天入口按钮不存在（不是地图页）
          2) 底部区域能找到左下的返回按钮形状 或 底部按钮模板得分 > 0.55
        """
        h, w = frame.shape[:2]
        # 还在聊天页（卡片可见）→ 肯定不是总攻页
        cards = _match_all(frame, self.tpl_card,
                           region=(int(w * 0.15), 0, w, int(h * 0.95)), thresh=0.70)
        if cards:
            return False
        # 底部是否有宽横条按钮（「已参与」模板即取自这个按钮，灰色状态也能匹配）
        strip = (int(w * 0.12), int(h * 0.85), int(w * 0.99), h)
        js, _, _, _, _ = _match(frame, self.tpl_joined, region=strip)
        if js >= 0.55:
            return True
        # 兜底：左下角返回按钮存在
        bs, _, _, _, _ = _match(frame, self.tpl_back,
                                region=(0, int(h * 0.85), int(w * 0.25), h))
        return bs >= 0.65

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

    def _close_chat(self, engine, w, h):
        """只在聊天页时点底部X关闭；已在活动地图页则不动。

        注意：活动地图底部中央的X是「退出海岛夺金活动」，绝不能点！
        判断方法：地图页右下角能找到聊天入口按钮 → 已回到地图，不用关。
        """
        try:
            frame = engine.screenshot()
            fh, fw = frame.shape[:2]
            rs, _, _, _, _ = _match(frame, self.tpl_chat,
                                    region=(int(fw * 0.76), int(fh * 0.68),
                                            fw, int(fh * 0.88)))
            if rs >= 0.62:
                return   # 已在地图页，什么都不点
            engine.tap(int(fw * 0.5), int(fh * 0.955))
            time.sleep(1.0)
        except Exception as e:
            self.on_log(f"[总攻] 关闭聊天异常: {e}")
