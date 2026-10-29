# -*- coding: utf-8 -*-
"""从参考截图按已知框提取 0-9 字形模板，保存到 assets/digits/。"""
import sys, cv2, numpy as np
from pathlib import Path

sys.path.insert(0, 'code/fishing_v023')
from mumu_io import MuMuIO

OUT = Path('code/fishing_v023/assets/digits')
OUT.mkdir(parents=True, exist_ok=True)

native = MuMuIO('mumu-ipc-2.json')
raw = native.screenshot()[:, :, :3].copy()


def white_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 0, 200), (180, 80, 255))
    m2 = cv2.inRange(hsv, (15, 0, 180), (45, 120, 255))
    return cv2.morphologyEx(m1 | m2, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


# (全分辨率 x0,y0,x1,y1, 数字)  —— 来自分割结果
BOXES = [
    (726, 709, 743, 737, '3'),
    (748, 709, 768, 737, '0'),
    (772, 709, 791, 737, '9'),
    (793, 709, 812, 737, '7'),
    (814, 709, 833, 738, '6'),
    (838, 709, 856, 737, '2'),
    (860, 709, 879, 737, '7'),
    (871, 1022, 890, 1050, '8'),
    (964, 1022, 981, 1050, '5'),
    (507, 1710, 528, 1739, '2'),
    (532, 1710, 550, 1739, '3'),
    (578, 1710, 600, 1739, '4'),
    (95, 1432, 132, 1482, '4'),
    (952, 1509, 962, 1534, '1'),
    (968, 1509, 979, 1534, '1'),
]

per_digit = {}
for x0, y0, x1, y1, d in BOXES:
    m = white_mask(raw[y0:y1, x0:x1])
    ys, xs = np.where(m > 0)
    if len(xs) == 0:
        print('EMPTY', d, x0, y0); continue
    g = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    per_digit.setdefault(d, []).append(g)

for d, glyphs in sorted(per_digit.items()):
    for i, g in enumerate(glyphs):
        p = OUT / f'd{d}_{i}.png'
        cv2.imwrite(str(p), g)
    print(f'digit {d}: {len(glyphs)} templates, heights {[g.shape[0] for g in glyphs]}')
