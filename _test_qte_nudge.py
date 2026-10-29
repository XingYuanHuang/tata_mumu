"""窄黄区"时机偏移搜索 + 命中学习"离线仿真。

模型（对齐真实游戏机制）：QTE 触点是固定屏幕位置，游戏判定的是按压时刻
鱼（游标）是否在判定窗内。控制器看到的帧有延迟，预测按压位置存在系统性
偏差 → 通过同区重试搜索（k: 0→+12→-12→+26→-26→+45→-45 px）找到命中时机，
命中后把 k 折算成时间校正量 aim_dt（EMA），下一个黄区第一 press 直接命中。
不发任何真实输入。"""
import sys, re, threading
sys.path.insert(0, 'code/fishing_v023')
from controller import Controller
from vision import Observation

VISUAL = (308.0, 338.0)        # 视觉黄区（宽30px，中心323）
EFFECTIVE = (326.0, 340.0)     # 游戏判定窗（鱼位置）：视觉中心偏右 10px
NEW_ZONE = (150.0, 181.0)      # 命中后切换的新黄区（视觉中心165.5）
NEW_EFFECTIVE = (160.0, 174.0) # 新区判定窗：同样偏右 ~8.5px


def tri(t):
    """150..450 三角波，游标速度 ±200px/s。"""
    p = (t * 200) % 600
    return 150 + p if p < 300 else 450 - (p - 300)


ctl = Controller(max_casts=99, bait_on=threading.Event())
t, zone, fires, hit_at = 0.0, VISUAL, [], None
while t < 40:
    t += 0.02
    obs = Observation('QTE', x=tri(t), target=zone, scores={'bar': 1.0})
    act = ctl.step(obs, t, 0.0, True)
    if act and act.kind == 'QTE' and 'deliberate' not in act.reason and 'watchdog' not in act.reason:
        ctl.input_sent(act, type('R', (), {'up_at': t})())
        k = int(re.search(r'k=([+-]\d+)', act.reason).group(1))
        fires.append((round(t, 2), round(tri(t), 1), k, act.reason))
        eff = EFFECTIVE if zone == VISUAL else NEW_EFFECTIVE
        if eff[0] <= tri(t) <= eff[1]:
            if zone == VISUAL:
                hit_at = len(fires)
                zone = NEW_ZONE            # 命中 → 游戏切换新黄区
            else:
                new_hit_at = len(fires)
                break

assert len(fires) >= 2, f'FAIL 点击次数不足: {fires}'
first_k = fires[0][2]
assert first_k == 0 and not (EFFECTIVE[0] <= fires[0][1] <= EFFECTIVE[1]), \
    f'FAIL 第一次点击(k=0)应被判定窗拒绝: {fires[0]}'
assert hit_at and 2 <= hit_at <= 6, f'FAIL 搜索未在6次内命中: {fires}'
hit_k = fires[hit_at - 1][2]
assert hit_k != 0, f'FAIL 命中应来自非零偏移: {fires[hit_at-1]}'
assert ctl.aim_dt != 0.0, f'FAIL 命中后未学习 aim_dt: {ctl.aim_dt}'
assert 0 < new_hit_at - hit_at <= 2, \
    f'FAIL 学习后新区应在2次内命中: {fires[hit_at:new_hit_at]}'
for f in fires[:new_hit_at]:
    print(f'PASS 尝试 t={f[0]}s 鱼位={f[1]} k={f[2]:+d} {f[3][-24:]}')
print(f'PASS 第 {hit_at} 次尝试命中 (k={hit_k:+d}) → aim_dt={ctl.aim_dt:.3f}s')
print(f'PASS 学习后新黄区第 {new_hit_at-hit_at} 次命中 → 校准生效')
print('ALL PASS')
