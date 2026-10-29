# -*- coding: utf-8 -*-
"""临时诊断：交互式导航回找朋友页，每步截图观察"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
import cv2
from mumu_io import MuMuIO
from visit_engine import VisitEngine

OUT = os.path.abspath("_shot")
m = MuMuIO('mumu-ipc-0.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: print('   ', s))

# 找朋友面板的打开方式：先试 BACK
for i in range(8):
    f = e.screenshot()
    st = e.detect_state(f)
    print(f'--- 第{i}步 state={st}')
    cv2.imwrite(os.path.join(OUT, f'nav2_{i}.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if st == 'search':
        print('>>> 已到找朋友页！停止')
        break
    e.key_event(4)
    time.sleep(2.0)

m.close()
print('DONE')
