# -*- coding: utf-8 -*-
"""临时诊断：① 新版主界面好友入口坐标确认 ② 点拜访后逐秒抓帧。"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
import cv2
from mumu_io import MuMuIO
from visit_engine import VisitEngine

UID = "13054369"
OUT = os.path.abspath("_shot_visit")
os.makedirs(OUT, exist_ok=True)

m = MuMuIO('mumu-ipc-1.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: print('   ', s))

def snap(tag):
    f = e.screenshot()
    p = os.path.join(OUT, f'{tag}.png')
    cv2.imwrite(p, f)
    st = e.detect_state(f)
    print(f'  [{tag}] state={st}')
    return f, st

print('--- 0. 初始状态 ---')
f, st = snap('00_init')
if st not in ('search', 'results'):
    print('  不在找朋友页，尝试右侧图标列（从下往上）')
    # 屏幕坐标（1080x1920 基准，按高度缩放适配其他分辨率）
    cands = [(997, 1629), (997, 1345), (540, 1880)]
    for i, (cx, cy) in enumerate(cands):
        ex, ey = e._scale_point(cx / (e.height * 1080 / 1920) * 0 + cx, cy)  # 直接用屏幕坐标
        # swipe/click 需要屏幕坐标，直接用 e.tap
        e.tap(int(cx * e.width / 1080), int(cy * e.height / 1920), hold_ms=120)
        time.sleep(1.8)
        f, st = snap(f'01_tap_right{i}')
        if st in ('search', 'results'):
            print(f'  ✓ 好友入口 = 屏幕坐标 ({cx},{cy})')
            break
    else:
        print('  !! 右侧三个图标都没打开好友面板，退出')
        m.close(); sys.exit(1)

print('--- 2. 输入 UID 并搜索 ---')
e.tap_exe(276, 559)
time.sleep(0.3)
e.clear_input()
e.input_text(UID)
time.sleep(0.3)
p = e.match(e.screenshot(), 'search_icon')
e.tap(p[0], p[1]) if p else e.tap_exe(532, 533)
time.sleep(2.0)
snap('02_results')

print('--- 3. 点头像，等拜访按钮 ---')
e.tap_exe(92, 660, hold_ms=130)
pos, frame = e._wait_template('visit', timeout=3, interval=0.1, wide=True)
print(f'  拜访按钮: {pos}')
snap('03_popup')

if pos:
    print('--- 4. 点拜访（模板中心原坐标），逐秒抓帧 12s ---')
    e.tap(pos[0], pos[1], hold_ms=130)
    for i in range(12):
        time.sleep(1.0)
        f2 = e.screenshot()
        st2 = e.detect_state(f2)
        c = e.match(f2, 'close', wide=True)
        v = e.match(f2, 'visit', wide=True)
        g = e.match(f2, 'gift', wide=True)
        t = e.match(f2, 'thumb')
        print(f'   t={i+1}s state={st2} close={None if not c else round(c[2],2)} '
              f'visit={None if not v else round(v[2],2)} gift={None if not g else round(g[2],2)} '
              f'thumb={None if not t else round(t[2],2)}')
        cv2.imwrite(os.path.join(OUT, f'04_after_{i+1:02d}.png'), f2)

print('--- 5. 收尾 ---')
e._return_to_find_friends()
snap('05_back')
m.close()
print('DONE')
