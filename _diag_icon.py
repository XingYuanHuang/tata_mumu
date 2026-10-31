# -*- coding: utf-8 -*-
"""临时诊断：输入完成后，验证「点放大镜」能否触发搜索"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
import cv2
from mumu_io import MuMuIO
from visit_engine import VisitEngine

OUT = os.path.abspath("_shot")
os.makedirs(OUT, exist_ok=True)

m = MuMuIO('mumu-ipc-0.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: print('   ', s))

def snap(tag):
    f = e.screenshot()
    cv2.imwrite(os.path.join(OUT, f'v3_{tag}.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 75])
    st = e.detect_state(f)
    print(f'  [{tag}] state={st}')
    return f, st

print('--- 0. 当前状态（输入框应已有 UID）---')
f, st = snap('00_cur')

if st != 'search':
    print('  不在 search 状态，退出')
    m.close(); raise SystemExit

pos = e.match(f, 'search_icon')
print('  search_icon 命中:', pos)
if pos is None:
    print('  未命中，退出')
    m.close(); raise SystemExit

print('--- 1. 点击放大镜触发搜索 ---')
e.tap(pos[0], pos[1])
time.sleep(1.0)
snap('01_tap_icon')

print('--- 2. 连续抓帧 12 秒 ---')
seen = []
for i in range(24):
    time.sleep(0.5)
    f2 = e.screenshot()
    st2 = e.detect_state(f2)
    seen.append(st2)
    if st2 != 'search' or i % 4 == 3:
        cv2.imwrite(os.path.join(OUT, f'v3_02_t{i*0.5+0.5:.1f}.jpg'), f2, [cv2.IMWRITE_JPEG_QUALITY, 75])
        print(f'   t={i*0.5+0.5:.1f}s state={st2}')
print('  末态:', seen[-1])

m.close()
print('DONE')
