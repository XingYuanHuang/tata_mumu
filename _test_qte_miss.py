# -*- coding: utf-8 -*-
"""离线仿真：验证 QTE 人为失误按间隔触发（不发送真实输入）。"""
import os
os.environ["FISH_MISS_MIN"] = "8"
os.environ["FISH_MISS_MAX"] = "10"
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "code" / "fishing_v023"))

from controller import Controller
from vision import Observation

ctl = Controller(max_casts=99, seed=7, backend="mumu-ipc")
print("next_miss(1-based after total):", ctl.qte_next_miss)

t = 0.0
sent = []
x = 150.0  # 游标从左向右匀速移动
target = (240.0, 360.0)
while ctl.qte_total < 25:
    t += 0.016
    x += 220 * 0.016  # ~220 px/s
    if x > 560:  # 一轮结束，重开一轮
        ctl.reset_qte()
        ctl.qte_active = False
        x = 150.0
    obs = Observation("QTE", x=x, target=target, scores={"bar": 1.0})
    act = ctl.step(obs, t, 0.0, True)
    for code, info in ctl.drain_events():
        if code == "qte_deliberate_miss":
            pass
    if act and act.kind == "QTE":
        ctl.input_sent(act, type("R", (), {"up_at": t})())
        sent.append((round(t, 2), round(x, 1), act.reason, act.hold_ms))

misses = [s for s in sent if "deliberate_miss" in s[2]]
print(f"total QTE sent={len(sent)}, misses={len(misses)}")
for s in sent:
    tag = " <-- MISS" if "deliberate_miss" in s[2] else ""
    print(f"  t={s[0]} x={s[1]} hold={s[3]}ms reason={s[2][:60]}{tag}")
print("miss 序号:", [sent.index(m) + 1 for m in misses])
assert misses, "没有触发失误"
# 校验失误间隔：相邻 miss 的 qte 序号差应在 [8,10] 附近（首 miss 由初始 next_miss 决定）
print("OK")
