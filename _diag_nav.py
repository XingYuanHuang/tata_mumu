# -*- coding: utf-8 -*-
"""临时诊断：从别人家园怎么回到找朋友页"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
import cv2
from mumu_io import MuMuIO
from visit_engine import VisitEngine

OUT = os.path.abspath("_shot")
m = MuMuIO('mumu-ipc-0.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: print('   ', s))

for i in range(6):
    f = e.screenshot()
    st = e.detect_state(f)
    hits = [f'{n}={r[2]:.2f}' for n in ['close','ok','search_icon','gift','results_title','best_title','choice_title'] for r in [e.match(f, n)] if r]
    print(f'--- 第{i}轮 state={st}  hits: {" ".join(hits)}')
    cv2.imwrite(os.path.join(OUT, f'nav_{i}.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if st == 'search':
        print('已回到找朋友页！')
        break
    pos = e.match(f, 'close')
    if pos:
        print('  点 close', pos)
        e.tap(pos[0], pos[1])
    else:
        print('  按 BACK')
        e.key_event(4)
    time.sleep(1.8)

f = e.screenshot()
print('最终 state =', e.detect_state(f))
cv2.imwrite(os.path.join(OUT, 'nav_final.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 70])
m.close()
