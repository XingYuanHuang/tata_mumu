# -*- coding: utf-8 -*-
"""QTE 卡顿诊断：只读抓帧，解剖 marker/target 识别每一步。不发任何输入。"""
import sys, time, json
sys.path.insert(0, 'code/fishing_v023')
from pathlib import Path
from collections import deque
import cv2
import numpy as np
from mumu_io import MuMuIO
from locator import Locator
from vision import Vision, WIDTH, HEIGHT

ROOT = Path(__file__).resolve().parent
native = MuMuIO(str(ROOT / 'mumu-ipc-2.json'))
locator = Locator(ROOT / 'code/fishing_v023/assets')
vision = Vision(ROOT / 'code/fishing_v023/assets')

raw = native.screenshot()[:, :, :3].copy()
found = locator.locate(raw)
if not found:
    print('LOCATE FAILED'); sys.exit(1)
region = found.region
print('region:', region.grab(), 'score=%.3f' % found.score, flush=True)

hsv_range_red = [(0,110,95),(13,255,255),(168,110,95),(179,255,255)]

def probe(frame):
    """复刻 vision QTE 分支并输出中间结果。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    scores = vision.template_scores(frame)
    out = {'bar': scores.get('bar'), 'bar_dx': scores.get('bar_dx'), 'bar_dy': scores.get('bar_dy'),
           'purple': None, 'markers': [], 'target': None, 'n_yellow_px': 0}
    button = hsv[802:916, 234:355]
    purple = float((cv2.inRange(button, (118,85,105), (170,255,255)) > 0).mean())
    out['purple'] = round(purple, 3)
    if scores['bar'] <= .73 or purple <= .15:
        return out, scores
    dy = int(scores.get('bar_dy', 0)); dx = int(scores.get('bar_dx', 0))
    region_ = hsv[660+dy:734+dy, 150+dx:456+dx]
    red = (cv2.inRange(region_, tuple(hsv_range_red[0]), tuple(hsv_range_red[1])) |
           cv2.inRange(region_, tuple(hsv_range_red[2]), tuple(hsv_range_red[3])))
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        ok = 7 <= w <= 34 and 12 <= h <= 65 and area > 70 and y < 42
        out['markers'].append(dict(x=150+dx+x+w/2, w=w, h=h, area=int(area), y=y, accept=ok))
    yellow = cv2.inRange(hsv[694+dy:713+dy, 145+dx:454+dx], (19,95,170), (39,255,255))
    out['n_yellow_px'] = int((yellow > 0).sum())
    xs = np.where(np.sum(yellow > 0, axis=0) >= 4)[0] + 145 + dx
    groups = np.split(xs, np.where(np.diff(xs) > 1)[0] + 1) if len(xs) else []
    out['yellow_groups'] = [(int(g[0]), int(g[-1]), len(g)) for g in groups if len(g) >= 3]
    return out, scores

samples = deque(maxlen=24)
end = time.time() + 75
n = 0
last_report = 0
state_count = {}
saved = 0
while time.time() < end:
    loop = time.time()
    raw = native.screenshot()[:, :, :3]
    frame = cv2.resize(raw, (WIDTH, HEIGHT))
    now = time.time()
    obs = vision.analyze(frame, now)
    state_count[obs.state] = state_count.get(obs.state, 0) + 1
    if obs.state == 'QTE':
        if saved < 6:
            cv2.imencode('.png', frame)[1].tofile(
                str(ROOT / 'debug_fishing' / f'qte_probe_{saved}.png'))
            saved += 1
    if obs.state == 'QTE' and not obs.flash:
        if obs.x is not None:
            samples.append((now, obs.x))
        p, scores = probe(frame)
        if now - last_report > 0.5:
            last_report = now
            vel = None
            if len(samples) >= 3:
                a = np.asarray(samples, float)
                d = np.diff(a[:, 1]); nz = d[np.abs(d) > .6]
                monotone = (np.any(nz > 0) and np.any(nz < 0)) is False
                vel = f'samples={len(samples)} span={a[-1,0]-a[0,0]:.3f}s monotone={monotone}'
            print(json.dumps(dict(
                t=now, x=obs.x, target=obs.target, vel_hint=vel,
                markers=p['markers'], yellow_groups=p['yellow_groups'],
                n_yellow=p['n_yellow_px'], bar=p['bar'], purple=p['purple']),
                ensure_ascii=False), flush=True)
    n += 1
    time.sleep(max(0, 0.03 - (time.time() - loop)))
print('DONE frames=', n, 'states=', state_count, flush=True)
