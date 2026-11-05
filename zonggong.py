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

# 总攻页按钮扫描参数（按钮都在页面右侧 0.58~0.95w 这一竖条上）
BTN_X1, BTN_X2 = 0.58, 0.95
BTN_MIN_ROW_PIX = 40    # 一行里命中像素数下限
BTN_MIN_BAND_H = 0.015  # 按钮带最小高度（占屏高比例），滤细噪声


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

    def __init__(self, engines_ref, on_log=None, poll_interval=3.0, check_interval=20.0):
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
        self._last_record = {}        # 卡死/错误记录节流：key -> 上次记录时间
        self.COOLDOWN_SEC = 20        # 参加一次后20秒内不再重复进（避免反复开关聊天）

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
            # 不在活动地图。若停在总攻详情/列表页（上轮返回没生效）→ 立即按返回，
            # 不等自愈；其他未知页面才走 miss 计数自愈。
            self._miss[name] = self._miss.get(name, 0) + 1
            bs, bxx, byy, bww, bhh = _match(frame, self.tpl_back,
                                            region=(0, int(h * 0.85), int(w * 0.25), h))
            if bs >= 0.60 and not _match_all(frame, self.tpl_card, thresh=0.70):
                engine.tap(bxx + bww // 2, byy + bhh // 2)
                self._miss[name] = 0
                self._record(name, "卡死：检测到停在总攻页，已按返回恢复")
                self.on_log(f"[总攻][{name}] 检测到停在总攻页，按返回恢复（已记录）")
                time.sleep(1.2)
                return
            if self._miss[name] >= 20:   # 连续~60秒不在地图
                self._miss[name] = 0
                self._record(name, "异常：连续60秒不在活动地图，触发自愈")
                self.on_log(f"[总攻][{name}] 连续60秒不在活动地图，尝试自愈返回（已记录）")
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

        # 3. 找可参与的总攻：直接扫聊天区里的绿色「参与」按钮条（最可靠信号）。
        #    灰色「已申请/已过期」不会命中绿色判据，天然跳过，不重复点击。
        #    实测：绿参与 BGR≈(53,216,149) 横贯约0.5w；灰已申请/已过期≈(160,160,160)。
        #    （不再依赖卡片模板选卡——黄绿底色卡片模板分只有0.58会漏检）
        btns = self._scan_chat_join_buttons(frame)
        n_cards = len(_match_all(frame, self.tpl_card,
                                 region=(int(w * 0.15), 0, w, int(h * 0.95)), thresh=0.65))
        if not btns:
            if n_cards:
                self.on_log(f"[总攻][{name}] {n_cards}张卡片均为已申请/已过期，无需操作，关闭聊天")
                self._cooldown[name] = now   # 20秒内不再开聊天（防红点反复开关）
                self._last_check[name] = now
            self._close_chat(engine, w, h)
            return

        # 从最底部（最新）的参与按钮开始尝试，最多点 3 个
        for idx, (yc, xc) in enumerate(btns[:3]):
            tag = "最新一个" if idx == 0 else f"倒数第{idx + 1}个"
            self.on_log(f"[总攻][{name}] 点击{tag}绿色参与按钮 y={yc:.3f}")
            engine.tap(int(xc * w), int(yc * h))
            # 轮询等待最多3.6秒：确认"已离开聊天页"（连续2帧既不是聊天页
            # 也不是地图页）= 已进入总攻页（详情页或列表页均可）。
            # 旧判据 _on_zonggong_page 认不出列表页（无返回键无底部条，实测
            # 返回False），导致列表页上的绿「参与」永远点不到 → 不点申请。
            entered = False
            consec = 0
            for _ in range(12):
                time.sleep(0.3)
                frame = engine.screenshot()
                h, w = frame.shape[:2]
                if self._is_chat_page(frame):
                    consec = 0
                    continue
                rs, *_ = _match(frame, self.tpl_chat,
                                region=(int(w * 0.76), int(h * 0.68), w, int(h * 0.88)))
                if rs >= 0.62:
                    consec = 0
                    continue
                consec += 1
                if consec >= 2:
                    entered = True
                    break
            if entered:
                time.sleep(0.8)   # 等页面渲染完再判定按钮颜色
                frame = engine.screenshot()
                h, w = frame.shape[:2]
                break
            self.on_log(f"[总攻][{name}] {tag}未进入总攻页，尝试下一个")
            # 可能还在聊天页，回到聊天继续点下一个
            self._close_chat(engine, w, h)
            time.sleep(0.8)
            frame = engine.screenshot()
            h, w = frame.shape[:2]
            if not _match_all(frame, self.tpl_card, thresh=0.65):
                if self._on_zonggong_page(frame):
                    self.on_log(f"[总攻][{name}] 页面加载较慢，现已进入总攻页")
                    break
                # 不在聊天页且不是总攻页：若停在总攻详情页（返回键可见）必须先返回，
                # 否则人会卡在申请页上（实测踩坑）
                bs, bxx, byy, bww, bhh = _match(frame, self.tpl_back,
                                                region=(0, int(h * 0.85), int(w * 0.25), h))
                if bs >= 0.60:
                    engine.tap(bxx + bww // 2, byy + bhh // 2)
                    self.on_log(f"[总攻][{name}] 停在总攻详情页，补按返回")
                    time.sleep(1.2)
                self.on_log(f"[总攻][{name}] 已离开聊天页，结束本轮")
                return
        else:
            self.on_log(f"[总攻][{name}] 所有卡片都未进入总攻页，关闭聊天")
            self._close_chat(engine, w, h)
            self._cooldown[name] = now
            return

        # 5. 总攻页有两种形态（点卡片后进哪个，取决于该地块有几条总攻）：
        #    a) 详情页（单条总攻）：底部一根横贯的大按钮（绿=未参与→点它）
        #    b) 列表页（同地块多条）：底部纯紫无按钮，右侧每条各带绿色参与按钮→扫描逐个点
        #    用底部条(0.88h~1h, 0.3w~0.9w)的颜色占比区分——绝不扫上半屏，
        #    详情页上半是实时地图（绿虚线/杀敌toast会误报，已实测踩坑）。
        #    绿>0.25=详情页未参与；灰>0.25=详情页已参与；两者都低=列表页。
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
            joined_cnt = 0
            for _ in range(6):                    # 列表页：最多点 6 个，防死循环
                frame = engine.screenshot()
                fh, fw = frame.shape[:2]
                actives = [(a, bnd) for a, bnd, act in self._scan_join_buttons(frame) if act]
                if not actives:
                    break
                a, bnd = actives[0]               # 每次点最上面的一个，点完重扫
                engine.tap(int(fw * 0.78), int((a + bnd) / 2 * fh))
                joined_cnt += 1
                self.on_log(f"[总攻][{name}] 列表页：点击参与按钮 y={(a + bnd) / 2:.3f}（第{joined_cnt}次）")
                time.sleep(0.9)
            self.on_log(f"[总攻][{name}] 列表页共点击参与 {joined_cnt} 次"
                        + ("（无绿色按钮，可能都已申请）" if joined_cnt == 0 else ""))

        # 6+7. 返回并验证循环：每轮都"截图判断→执行点击"，直到确认回到活动地图（上限4轮）。
        #       注意：点击动作必须在循环体内，每轮都做；之前 for-else 写错导致
        #       循环只截图不点击，4次"卡死"日志全是假报警（返回键根本没按过，11-02踩坑）。
        returned = False
        for attempt in range(4):
            frame = engine.screenshot()
            h, w = frame.shape[:2]
            # 已在活动地图（右下角聊天入口可见）→ 完成
            rs, *_ = _match(frame, self.tpl_chat,
                            region=(int(w * 0.76), int(h * 0.68), w, int(h * 0.88)))
            if rs >= 0.62:
                returned = True
                if attempt:
                    self.on_log(f"[总攻][{name}] 已确认返回活动地图（操作了{attempt}次）")
                break
            # 返回键可见 → 在总攻详情/列表页，按返回
            bs, bxx, byy, bww, bhh = _match(frame, self.tpl_back,
                                            region=(0, int(h * 0.85), int(w * 0.25), h))
            if bs >= 0.60:
                engine.tap(bxx + bww // 2, byy + bhh // 2)
                self.on_log(f"[总攻][{name}] 仍在总攻页，按返回（第{attempt + 1}次）")
            else:
                # 聊天页 → 点底部X；其他未知页面也点X（比乱按返回安全）
                engine.tap(int(w * 0.5), int(h * 0.955))
                self.on_log(f"[总攻][{name}] 不在总攻页，点底部X（第{attempt + 1}次，chat_btn={rs:.2f}）")
            time.sleep(1.2)
        if not returned:
            # 卡死现场存图，方便事后定位
            try:
                stuck_dir = Path(__file__).parent / "debug_zg_stuck"
                stuck_dir.mkdir(exist_ok=True)
                cv2.imwrite(str(stuck_dir / f"stuck_{time.strftime('%m%d_%H%M%S')}.jpg"), frame)
            except Exception:
                pass
            self._record(name, "卡死：4轮返回/关X后仍未回到活动地图（已存现场截图）")
            self.on_log(f"[总攻][{name}] 卡死：4轮后仍未回地图（已记录+存图）")
        self._cooldown[name] = time.time()
        self.on_log(f"[总攻][{name}] 本轮流程完成，继续监听")

    def _scan_join_buttons(self, frame):
        """扫描总攻页右侧按钮列，返回 [(y0比例, y1比例, active), ...]（按y升序）。

        active=True  → 绿色「参与」按钮（亮绿 BGR≈(60,204,132)）
        active=False → 灰色「已申请」按钮（中性灰 ≈(144,144,144)）
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

    def _scan_chat_join_buttons(self, frame):
        """扫描聊天页中的绿色「参与」按钮条（聊天卡片正下方）。

        返回 [(y中心比例, x中心比例), ...]，按y降序（最底部=最新优先）。
        实测颜色：绿参与 BGR≈(53,216,149)，横贯约0.5w；灰「已申请/已过期」
        ≈(160,160,160) 不满足绿色判据，天然不会命中。
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

    def _is_chat_page(self, frame):
        """判断当前是否在聊天页。

        聊天页独有左侧深色频道栏（x 0.02~0.16）。实测深色像素占比：
        聊天页 0.57，列表页 0.14，详情页 0.20，地图 0.34——阈值 0.45 干净分离。
        （卡片模板已失效：现版聊天卡片 tpl_card 匹配不到，不能用来判聊天页）
        """
        h, w = frame.shape[:2]
        sub = frame[int(0.10 * h):int(0.85 * h), int(0.02 * w):int(0.16 * w)]
        dark = (sub.max(axis=2) < 90).mean()
        return dark >= 0.45

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
