"""White digit recognition on the fishing page (level badge, bait counter).

Templates are binarized glyphs cropped from real game screenshots
(same in-game font). Matching runs on the full-resolution region crop,
never on the 588x968 preview, so small counters stay readable.
"""
from __future__ import annotations
from pathlib import Path
import cv2
import numpy as np

# 模板统一归一化到该高度后比较。
NORM_H = 40


def white_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 0, 200), (180, 80, 255))
    m2 = cv2.inRange(hsv, (15, 0, 180), (45, 120, 255))
    return cv2.morphologyEx(m1 | m2, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


class DigitReader:
    def __init__(self, assets: Path):
        self.templates = []  # (digit, normalized mask)
        for p in sorted(Path(assets, 'digits').glob('d*.png')):
            m = cv2.imread(str(p), 0)
            if m is None:
                continue
            digit = p.name[1]
            h = NORM_H
            w = max(2, int(round(m.shape[1] * h / m.shape[0])))
            self.templates.append(
                (digit, cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA) > 127))

    def _classify(self, glyph_mask):
        best, best_digit = 0.0, None
        h = NORM_H
        for digit, templ in self.templates:
            w = max(2, int(round(glyph_mask.shape[1] * h / glyph_mask.shape[0])))
            g = cv2.resize(glyph_mask, (w, h), interpolation=cv2.INTER_AREA) > 127
            W = max(w, templ.shape[1])
            a = np.zeros((h, W), bool); a[:, :w] = g
            b = np.zeros((h, W), bool); b[:, :templ.shape[1]] = templ
            inter = (a & b).sum(); union = (a | b).sum()
            score = inter / union if union else 0.
            if score > best:
                best, best_digit = score, digit
        return best_digit, best

    def _glyphs(self, bgr, min_h, max_h):
        m = white_mask(bgr)
        n, _, stats, _ = cv2.connectedComponentsWithStats(m)
        boxes = []
        for i in range(1, n):
            x, y, w, h, a = stats[i]
            if min_h <= h <= max_h and a > 25 and w >= 3:
                boxes.append([x, y, x + w, y + h])
        boxes.sort(key=lambda b: b[0])
        merged = []
        for b in boxes:
            if merged and b[0] <= merged[-1][2] - 2 and b[1] < merged[-1][3] - min_h * .5:
                q = merged[-1]
                q[0] = min(q[0], b[0]); q[1] = min(q[1], b[1])
                q[2] = max(q[2], b[2]); q[3] = max(q[3], b[3])
            else:
                merged.append(list(b))
        out = []
        for x0, y0, x1, y1 in merged:
            g = white_mask(bgr[y0:y1, x0:x1])
            ys, xs = np.where(g)
            if len(xs):
                out.append((x0 + xs.min(), g[ys.min():ys.max() + 1, xs.min():xs.max() + 1]))
        return out

    def read(self, bgr, min_h=14, max_h=70, max_digits=9, min_score=.55):
        """读取一段区域里的白色数字，返回 int 或 None（识别失败）。"""
        if not self.templates:
            return None
        glyphs = self._glyphs(bgr, min_h, max_h)
        if not glyphs or len(glyphs) > max_digits:
            return None
        text = ''
        for _, g in glyphs:
            digit, score = self._classify(g)
            if digit is None or score < min_score:
                return None
            text += digit
        try:
            return int(text)
        except ValueError:
            return None
