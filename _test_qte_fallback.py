"""QTE 看门狗兜底离线仿真：不发送任何真实输入。"""
import sys, threading
sys.path.insert(0, 'code/fishing_v023')
from controller import Controller
from vision import Observation

def run(name, frames, expect_reason, expect_window, dt=0.055):
    ctl = Controller(max_casts=99, bait_on=threading.Event())
    t = 0.0
    fired = None
    i = 0
    while t < 40 and fired is None:
        t += dt
        obs = frames(t, i)
        i += 1
        act = ctl.step(obs, t, 0.0, True)
        if act and act.kind == 'QTE':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            fired = (round(t, 2), act.reason)
    assert fired, f'{name}: FAIL 未触发任何点击'
    assert expect_reason in fired[1], f'{name}: FAIL reason={fired[1]}'
    assert expect_window[0] <= fired[0] <= expect_window[1], \
        f'{name}: FAIL 触发时刻 {fired[0]}s 不在 {expect_window}'
    print(f'PASS {name}: t={fired[0]}s reason={fired[1][:52]}')
    return ctl

# 场景1：识别全失败（x/target 均为 None，但状态仍是 QTE）→ 二级盲点兜底
run('识别全失败 → 盲点兜底',
    lambda t, i: Observation('QTE', scores={'bar': 1.0}),
    'watchdog(blind)', (7.4, 9.0))

# 场景2：浮标在黄区内但处于 12% 缓冲带（slow_drift 不触发）、速度抖动估计不出
#        → 一级黄区内兜底（~5s）
def jitter_in_margin(t, i):
    x = 247.0 + (1.5 if i % 2 else -1.5)   # 目标 240~360，slow_drift 需 >254.4
    return Observation('QTE', x=x, target=(240.0, 360.0), scores={'bar': 1.0})
run('黄区边缘缓冲带 → 一级兜底',
    jitter_in_margin, 'watchdog(inside)', (4.9, 6.5))

# 场景3：快速轮回归——正常预测点击必须先于看门狗（远早于 5s）
def fast_wheel(t, i):
    return Observation('QTE', x=100.0 + t*190, target=(240.0, 360.0), scores={'bar': 1.0})
run('快速轮回归（正常点击优先）', fast_wheel, 'speed=', (0.1, 2.0), dt=0.016)

# 场景4：慢速漂移轮回归——slow_drift（1.2s）必须先于看门狗（5s）
run('慢速轮回归（slow_drift 优先）',
    lambda t, i: Observation('QTE', x=300.0, target=(240.0, 360.0), scores={'bar': 1.0}),
    'slow_drift', (1.0, 2.5))

# 场景5：盲点失败后 0.9s 宽限解锁 → 看门狗周期性重试，不终止会话
ctl = Controller(max_casts=99, bait_on=threading.Event())
t = 0.0; clicks = []
while t < 30:
    t += 0.055
    obs = Observation('QTE', scores={'bar': 1.0})   # 永远识别不出 x/target
    act = ctl.step(obs, t, 0.0, True)
    if act and act.kind == 'QTE':
        ctl.input_sent(act, type('R', (), {'up_at': t})())
        clicks.append((round(t, 1), act.reason.split(' ')[0]))
    assert not (isinstance(act, dict)) , ''
stopped = None
assert len(clicks) >= 3, f'FAIL 盲点重试次数不足: {clicks}'
gaps = [round(clicks[k+1][0]-clicks[k][0], 1) for k in range(len(clicks)-1)]
print(f'PASS 盲点失败持续重试: {len(clicks)} 次点击 {clicks[:4]}... 间隔 {gaps[:3]}...')
print('ALL PASS')
