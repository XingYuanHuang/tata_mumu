# -*- coding: utf-8 -*-
"""临时诊断：对照测试 nemu IPC 点击 vs adb 点击 是否真正生效"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
import cv2
from mumu_io import MuMuIO
from visit_engine import VisitEngine

OUT = os.path.abspath("_shot")
os.makedirs(OUT, exist_ok=True)

m = MuMuIO('mumu-ipc-0.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: None)

def snap(tag):
    f = e.screenshot()
    cv2.imwrite(os.path.join(OUT, f'tap_{tag}.jpg'), f, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return f

# 输入框中心（避开右侧放大镜）：图像(613x1080)上约 (270, 583) → 实际 (480, 1036)
FX, FY = 480, 1036

print('=== 0. 初始 ===')
f0 = snap('00')
print('  shape', f0.shape)

print('=== 1. nemu IPC 点击输入框 ===')
e.tap(FX, FY)
time.sleep(1.5)
f1 = snap('01_ipc_tap')
diff1 = abs(f1.astype(int) - f0.astype(int)).mean()
print(f'  与初始帧平均像素差 = {diff1:.2f}  (>1 说明画面有变化)')

print('=== 2. adb 点击输入框 ===')
r = e._adb_device('shell', 'input', 'tap', FX, FY, timeout=10)
print('  返回码 =', r.returncode, r.stderr.decode(errors='ignore')[:60])
time.sleep(1.5)
f2 = snap('02_adb_tap')
diff2 = abs(f2.astype(int) - f1.astype(int)).mean()
print(f'  与上一帧平均像素差 = {diff2:.2f}  (>1 说明画面有变化)')

print('=== 3. 有键盘的话输入 1 个数字 ===')
r = e._adb_device('shell', 'input', 'text', '1', timeout=10)
print('  input text 返回码 =', r.returncode)
time.sleep(1.0)
f3 = snap('03_text')
diff3 = abs(f3.astype(int) - f2.astype(int)).mean()
print(f'  与上一帧平均像素差 = {diff3:.2f}')

m.close()
print('DONE')
