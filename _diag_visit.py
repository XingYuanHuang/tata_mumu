# -*- coding: utf-8 -*-
"""临时诊断：验证修复后的搜索链路（点输入框→输入UID→回车→结果页）"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
import cv2
from mumu_io import MuMuIO
from visit_engine import VisitEngine

UID = "19619531"
OUT = os.path.abspath("_shot")
os.makedirs(OUT, exist_ok=True)

m = MuMuIO('mumu-ipc-0.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: print('   ', s))

def snap(tag):
    f = e.screenshot()
    cv2.imwrite(os.path.join(OUT, f'v2_{tag}.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 75])
    st = e.detect_state(f)
    print(f'  [{tag}] state={st}')
    return f, st

print('--- 0. 初始 ---')
f, st = snap('00_init')
if st != 'search':
    print('  不在找朋友页，先返回')
    e._return_to_find_friends()
    time.sleep(1.5)
    f, st = snap('00b_back')

print('--- 1. 点击输入框 (exe 276,559) ---')
e.tap_exe(276, 559)
time.sleep(1.2)
snap('01_focus')

print('--- 2. 清空 + 输入 UID ---')
t = time.time()
e.clear_input()
e.input_text(UID)
print(f'  输入耗时 {time.time()-t:.2f}s')
time.sleep(1.2)
snap('02_typed')

print('--- 3. 回车搜索 ---')
e.key_event(66)
time.sleep(0.5)
e.key_event(66)
time.sleep(0.5)
snap('03_enter')

print('--- 4. 连续抓帧 12 秒 ---')
seen = []
for i in range(24):
    time.sleep(0.5)
    f2 = e.screenshot()
    st2 = e.detect_state(f2)
    seen.append(st2)
    if st2 != (seen[-2] if len(seen) >= 2 else None) or i % 4 == 3:
        cv2.imwrite(os.path.join(OUT, f'v2_04_t{i*0.5+0.5:.1f}.jpg'), f2, [cv2.IMWRITE_JPEG_QUALITY, 75])
        print(f'   t={i*0.5+0.5:.1f}s state={st2}')
print('  状态序列:', set(seen), '| 末态:', seen[-1])

m.close()
print('DONE')
