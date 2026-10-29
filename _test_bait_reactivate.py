"""打窝等级衰减后重新激活撒饵：离线仿真，不发真实输入。"""
import sys, threading
sys.path.insert(0, 'code/fishing_v023')
from controller import Controller
from vision import Observation

ctl = Controller(max_casts=99, bait_on=threading.Event())
ctl.bait_on.set()
events, fired, cast_at = [], None, None
t = 0.0
while t < 25:
    t += 0.2
    if cast_at is not None and t - cast_at < 1.5:
        obs = Observation('WAIT', scores={'title': 1.0})   # 抛竿后游戏进入等待
    else:
        level = 4 if t > 6.0 else 8                        # 6 秒后模拟打窝等级衰减
        obs = Observation('READY', level=level, bait_count=9, scores={'title': 1.0})
    act = ctl.step(obs, t, 0.0, True)
    if act:
        if act.kind == 'CAST':
            cast_at = t
        if act.kind == 'BAIT' and fired is None:
            fired = t
    events += [e[0] for e in ctl.drain_events()]

assert any(e == 'bait_done' for e in events), f'FAIL 未出现 bait_done: {events}'
assert any(e == 'bait_reactivate' for e in events), \
    f'FAIL 等级衰减后未重新激活撒饵: {events}'
assert fired, f'FAIL 重新激活后未发出 BAIT 点击: {events}'
print(f'PASS 等级8→bait_done → 衰减到4→bait_reactivate → BAIT 点击 t={fired}s')
print('ALL PASS')
