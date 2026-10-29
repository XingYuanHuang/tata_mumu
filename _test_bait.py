# -*- coding: utf-8 -*-
"""撒鱼饵功能离线仿真：决策逻辑 + 跨实例门闩。不发送任何真实输入。"""
import sys, time, threading
sys.path.insert(0, 'code/fishing_v023')
from controller import Controller, BAIT_POINT, BAIT_LEVEL_MAX
from vision import Observation
from bait_gate import BaitGate


def obs(state='READY', level=4, bait=11, popup=False):
    o = Observation(state, scores={'green': 1.0}, level=level, bait_count=bait)
    if popup:
        o.bait_popup = True
        o.control = (460.0, 470.0)   # 打窝按钮中心（画布坐标）
    return o


def run_bait_scenario(name, level, bait, expect_baits):
    """喂 READY 观测：BAIT 点鱼饵包 → 弹窗出现 → BAIT2 打窝 → 鱼饵-1 确认。"""
    ctl = Controller(max_casts=5, bait_on=threading.Event())
    ctl.bait_on.set()
    t = 0.0
    lv, bt = level, bait
    baits, casts = 0, 0
    popup = False
    for _ in range(120):
        t += 0.1
        act = ctl.step(obs('READY', lv, bt, popup), t)
        if act is None:
            continue
        if act.kind == 'BAIT':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            popup = True                      # 点鱼饵包后弹窗弹出
        elif act.kind == 'BAIT2':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            baits += 1
            bt -= 1                           # 打窝生效：鱼饵包数量减少
            popup = False                     # 弹窗关闭
        elif act.kind == 'CAST':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            casts += 1
            # CAST 确认需两帧去抖：第一帧置 confirm_since，第二帧通过
            ctl._pending_step(obs('WAIT'), t + 0.1, True)
            ctl._pending_step(obs('WAIT'), t + 0.2, True)
        elif act.kind == 'STOP':
            break
    ok = baits == expect_baits and casts >= 1
    print(f"[{'OK ' if ok else 'FAIL'}] {name}: baits={baits} (期望 {expect_baits}), casts={casts}, "
          f"bait_done={ctl.bait_done}")
    return ok


ok1 = run_bait_scenario('等级4鱼饵11 → 撒到鱼饵为0(11次)', 4, 11, 11)
def run_level_scenario():
    ctl = Controller(max_casts=5, bait_on=threading.Event())
    ctl.bait_on.set()
    t = 0.0; lv, bt = 4, 11
    baits, casts = 0, 0
    popup = False
    for _ in range(160):
        t += 0.1
        act = ctl.step(obs('READY', lv, bt, popup), t)
        if act is None: continue
        if act.kind == 'BAIT':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            popup = True
        elif act.kind == 'BAIT2':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            baits += 1; lv += 1; bt -= 1  # 打窝生效：等级+1、鱼饵-1
            popup = False
        elif act.kind == 'CAST':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            casts += 1
            ctl._pending_step(obs('WAIT'), t + 0.1, True)
            ctl._pending_step(obs('WAIT'), t + 0.2, True)
        elif act.kind == 'STOP':
            break
    ok = baits == 4 and casts >= 1 and ctl.bait_done
    print(f"[{'OK ' if ok else 'FAIL'}] 等级每次+1: baits={baits} (期望4), casts={casts}, bait_done={ctl.bait_done}")
    return ok


def run_popup_already_open():
    """弹窗已开（如上次点击残留）：第一个动作就应是 BAIT2 直接点打窝。"""
    ctl = Controller(max_casts=5, bait_on=threading.Event())
    ctl.bait_on.set()
    t = 1.0
    a = ctl.step(obs('READY', 4, 12, popup=True), t)
    ok = a is not None and a.kind == 'BAIT2'
    print(f"[{'OK ' if ok else 'FAIL'}] 弹窗残留直接打窝: first={a.kind if a else None}")
    return ok


def run_no_bait_scenario():
    ctl = Controller(max_casts=5, bait_on=threading.Event())
    ctl.bait_on.set()
    t = 0.0
    acts = []
    for _ in range(20):
        t += 0.1
        act = ctl.step(obs('READY', 4, 0), t)  # 鱼饵为 0
        if act:
            acts.append(act.kind)
            if act.kind == 'STOP':
                break
    # 二次确认：第一帧 pending（先抛竿），第二帧才 bait_done
    ok = 'BAIT' not in acts and 'CAST' in acts and ctl.bait_done
    print(f"[{'OK ' if ok else 'FAIL'}] 鱼饵为0: actions={acts[:4]}..., bait_done={ctl.bait_done}")
    return ok


def run_switch_off():
    ctl = Controller(max_casts=5, bait_on=threading.Event())
    ctl.bait_on.set()
    t = 1.0
    ctl.step(obs('READY', 4, 11), t)   # 首帧：状态切换，无动作
    t += 0.5
    a1 = ctl.step(obs('READY', 4, 11), t)
    ctl.bait_on.clear()  # 运行中关闭
    t += 0.5
    a2 = ctl.step(obs('READY', 4, 11), t)
    ok = a1 and a1.kind == 'BAIT' and a2 and a2.kind == 'CAST'
    print(f"[{'OK ' if ok else 'FAIL'}] 运行中关开关: first={a1.kind if a1 else None}, after_off={a2.kind if a2 else None}")
    return ok


def run_gate():
    """两个"实例"的门闩在同一目录竞争，验证串行 + 间隔 >= gap。"""
    import os, tempfile
    d = tempfile.mkdtemp()
    gap = 0.8
    clicks = []
    def worker(i):
        g = BaitGate(d, gap=gap, timeout=10)
        t0 = time.monotonic()
        if g.wait_turn(None):
            clicks.append((i, time.monotonic() - t0, time.monotonic()))
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for th in ths: th.start()
    for th in ths: th.join()
    clicks.sort(key=lambda c: c[2])
    gaps_ok = all(clicks[k+1][2] - clicks[k][2] >= gap - 0.05 for k in range(len(clicks)-1))
    ok = len(clicks) == 3 and gaps_ok
    print(f"[{'OK ' if ok else 'FAIL'}] 门闩3实例竞争: order={[c[0] for c in clicks]}, "
          f"gaps={[round(clicks[k+1][2]-clicks[k][2],2) for k in range(len(clicks)-1)]}")
    return ok


r0 = run_popup_already_open()
r1 = run_level_scenario()
r2 = run_no_bait_scenario()
r3 = run_switch_off()
r4 = run_gate()
print('\nALL', 'PASS' if all([r0, r1, r2, r3, r4]) else 'FAIL')
