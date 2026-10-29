# -*- coding: utf-8 -*-
"""临时诊断：结果页点头像 → 资料页 → 拜访按钮，逐步截图确认坐标"""
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
    cv2.imwrite(os.path.join(OUT, f'v4_{tag}.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 75])
    st = e.detect_state(f)
    hits = [f'{n}={r[2]:.2f}@({r[0]},{r[1]})' for n in
            ['results_title','visit','gift','best_title','choice_title','honors','thumb','choose_button','close','ok']
            for r in [e.match(f, n)] if r]
    print(f'  [{tag}] state={st}  {" ".join(hits)}')
    return f, st

f, st = snap('00_results')
if st != 'results':
    print('当前不在结果页，退出'); m.close(); raise SystemExit

print('--- 1. 点击头像 (exe 92,660) ---')
e.tap_exe(92, 660)
time.sleep(2.0)
f, st = snap('01_profile')

print('--- 2. 再等2秒看是否是资料页 ---')
time.sleep(2.0)
f, st = snap('02_profile2')

# 如果有 visit 模板命中，点它
pos = e.match(f, 'visit')
if pos:
    print(f'--- 3. 点击拜访按钮 {pos} ---')
    e.tap(pos[0], pos[1])
    time.sleep(2.5)
    f, st = snap('03_after_visit')
else:
    print('--- 3. visit 模板未命中，先看画面 ---')

m.close()
print('DONE')
