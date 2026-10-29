"""小黄区中心瞄准离线仿真：不发送任何真实输入。"""
import sys, re, threading
sys.path.insert(0, 'code/fishing_v023')
from controller import Controller
from vision import Observation


def fire(ctl, frames, dt=0.016, tmax=30):
    t, i = 0.0, 0
    while t < tmax:
        t += dt
        act = ctl.step(frames(t, i), t, 0.0, True)
        i += 1
        if act and act.kind == 'QTE':
            ctl.input_sent(act, type('R', (), {'up_at': t})())
            return t, act.reason
    return None, None


# 场景1：30px 小黄区，游标 200px/s 从左向右穿过（复现日志 308..338 场景）
ctl = Controller(max_casts=99, bait_on=threading.Event())
t, reason = fire(ctl, lambda t, i: Observation(
    'QTE', x=100.0 + t*200, target=(308.0, 338.0), scores={'bar': 1.0}))
assert t, 'FAIL 未触发点击'
down = float(re.search(r'down=([0-9.]+)', reason).group(1))
up = float(re.search(r'up=([0-9.]+)', reason).group(1))
mid = (down + up) / 2
assert 312.2 <= down <= 333.8 and 312.2 <= up <= 333.8 and abs(mid-323) <= 5, \
    f'FAIL 小黄区未命中中心: down={down} up={up} mid={mid:.1f}'
print(f'PASS 小黄区中心瞄准: down={down} up={up} mid={mid:.1f} (中心323, 旧逻辑会落在~333贴边)')

# 场景2：30px 小黄区，反向（v<0）从右向左
ctl = Controller(max_casts=99, bait_on=threading.Event())
t, reason = fire(ctl, lambda t, i: Observation(
    'QTE', x=450.0 - t*200, target=(308.0, 338.0), scores={'bar': 1.0}))
assert t, 'FAIL 未触发点击'
down = float(re.search(r'down=([0-9.]+)', reason).group(1))
up = float(re.search(r'up=([0-9.]+)', reason).group(1))
mid2=(down+up)/2
assert 312.2 <= down <= 333.8 and 312.2 <= up <= 333.8 and abs(mid2-323) <= 5, \
    f'FAIL 反向小黄区未命中中心: down={down} up={up} mid={mid2:.1f}'
print(f'PASS 反向小黄区中心瞄准: down={down} up={up} mid={mid2:.1f}')

# 场景3：帧率退化（dt=0.045s，三实例抢占时）也应命中中心附近而非出界
ctl = Controller(max_casts=99, bait_on=threading.Event())
t, reason = fire(ctl, lambda t, i: Observation(
    'QTE', x=100.0 + t*200, target=(308.0, 338.0), scores={'bar': 1.0}), dt=0.045)
assert t, 'FAIL 未触发点击'
down = float(re.search(r'down=([0-9.]+)', reason).group(1))
up = float(re.search(r'up=([0-9.]+)', reason).group(1))
mid3=(down+up)/2
assert 312.2 <= down <= 333.8 and 312.2 <= up <= 333.8 and abs(mid3-323) <= 8, \
    f'FAIL 低帧率下出界: down={down} up={up} mid={mid3:.1f}'
print(f'PASS 低帧率(dt=45ms)仍安全: down={down} up={up} mid={mid3:.1f}')

# 场景4：大黄区回归——仍应尽早点击（1s 内），不被中心等待拖慢
ctl = Controller(max_casts=99, bait_on=threading.Event())
t, reason = fire(ctl, lambda t, i: Observation(
    'QTE', x=100.0 + t*190, target=(240.0, 360.0), scores={'bar': 1.0}))
assert t and t < 1.2, f'FAIL 大区点击被拖慢: t={t}'
print(f'PASS 大黄区回归（尽早点击 t={t:.2f}s）: {reason[:60]}')
print('ALL PASS')
