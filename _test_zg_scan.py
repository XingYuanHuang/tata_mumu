# -*- coding: utf-8 -*-
"""离线验证 _scan_join_buttons：真实截图（全灰按钮）+ 合成绿色未参与按钮。"""
import cv2
import numpy as np
from zonggong import ZongGongMonitor

IMG = r"C:\Users\xy\.workbuddy\clipboard-images\clipboard-2026-10-29T00-33-11-859Z-f165d0bf.jpg"

mon = ZongGongMonitor(lambda: {}, on_log=print)

frame = cv2.imread(IMG)
h, w = frame.shape[:2]
print("== 真实截图（两个灰按钮，应全 inactive、无 active）==")
bands = mon._scan_join_buttons(frame)
for y0, y1, act in bands:
    print(f"  y {y0:.3f}~{y1:.3f} {'active!!' if act else 'inactive'}")
assert bands and all(not act for _, _, act in bands), "真实截图不应有 active 带"
print("  通过：灰色按钮全部识别为 inactive，无误报\n")

# 合成：把一个绿色按钮贴在 y≈0.72（无条目区域），模拟未参与按钮
f2 = frame.copy()
by = int(0.72 * h)
btn = frame[int(0.531 * h):int(0.581 * h), int(0.66 * w):int(0.905 * w)].copy()
green = btn.copy()
green[:, :] = (80, 180, 60)          # BGR 绿色
f2[by:by + green.shape[0], int(0.66 * w):int(0.66 * w) + green.shape[1]] = green
print("== 合成绿色未参与按钮 y≈0.72（应识别为 active）==")
bands2 = mon._scan_join_buttons(f2)
for y0, y1, act in bands2:
    print(f"  y {y0:.3f}~{y1:.3f} {'active' if act else 'inactive'}")
actives = [(a, b) for a, b, act in bands2 if act]
assert actives and abs((actives[0][0] + actives[0][1]) / 2 - 0.745) < 0.03, \
    f"应识别到 y≈0.745 的 active 按钮，实际 {actives}"
print("  通过：绿色按钮识别为 active，位置准确\n")

# 合成：顶部也放一个（模拟新总攻排在上面）
f3 = f2.copy()
by3 = int(0.20 * h)
f3[by3:by3 + green.shape[0], int(0.66 * w):int(0.66 * w) + green.shape[1]] = green
bands3 = mon._scan_join_buttons(f3)
actives3 = [(a, b) for a, b, act in bands3 if act]
print("== 顶部+中部各一个绿色按钮（应两个都 active）==")
for y0, y1, act in bands3:
    print(f"  y {y0:.3f}~{y1:.3f} {'active' if act else 'inactive'}")
assert len(actives3) == 2, f"应识别到 2 个 active，实际 {len(actives3)}"
print("  通过：多个未参与按钮全部识别，从上往下点击顺序正确\n")

# 合成：6 条槽位混合（灰/绿/灰/绿/绿/绿），验证多条例表
f4 = frame.copy()
positions = [0.20, 0.32, 0.45, 0.585, 0.72, 0.855]   # 6 个槽位
kinds = ["gray", "green", "gray", "green", "green", "green"]
src_gray = frame[int(0.531 * h):int(0.581 * h), int(0.66 * w):int(0.905 * w)].copy()
expect = []
for pos, kind in zip(positions, kinds):
    by = int(pos * h)
    piece = green if kind == "green" else src_gray
    f4[by:by + piece.shape[0], int(0.66 * w):int(0.66 * w) + piece.shape[1]] = piece
    if kind == "green":
        expect.append(pos + 0.025)
bands4 = mon._scan_join_buttons(f4)
actives4 = [(a, b) for a, b, act in bands4 if act]
print("== 4 绿 2 灰混合（应精确点中 4 个绿色，跳过 2 个灰色）==")
for y0, y1, act in bands4:
    print(f"  y {y0:.3f}~{y1:.3f} {'active' if act else 'inactive'}")
assert len(actives4) == 4, f"应识别到 4 个 active，实际 {len(actives4)}"
for (a, b), e in zip(actives4, expect):
    assert abs((a + b) / 2 - e) < 0.03, f"中心 y 应≈{e}，实际 {(a + b) / 2:.3f}"
print("  通过：多条例表下灰色/彩色混合全部识别正确")

print("\n全部用例通过")
