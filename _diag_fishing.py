# -*- coding: utf-8 -*-
"""钓鱼功能实况调试：连实例3，后台截图 + 定位 + 状态识别（只读，不发输入）。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "code" / "fishing_v023"))

import cv2
import numpy as np
from mumu_io import MuMuIO
from locator import Locator, Region
from vision import Vision, annotate, WIDTH, HEIGHT

IPC = ROOT / "mumu-ipc-2.json"  # 实例3 = index 2
OUT = ROOT / "debug_fishing"
OUT.mkdir(exist_ok=True)


def main():
    native = MuMuIO(str(IPC))
    try:
        print("resolution:", native.width, native.height, "serial:", native.serial)
        ts = time.strftime("%H%M%S")
        raw = native.screenshot()[:, :, :3].copy()  # BGRA -> BGR，与 runtime 一致
        cv2.imencode(".png", raw)[1].tofile(str(OUT / f"raw_{ts}.png"))
        print("screenshot saved:", OUT / f"raw_{ts}.png", raw.shape)

        locator = Locator(ROOT / "code" / "fishing_v023" / "assets")
        vision = Vision(ROOT / "code" / "fishing_v023" / "assets")

        h, w = raw.shape[:2]
        found = locator.locate(raw)
        if found is None:
            print("LOCATE: FAILED (未找到钓鱼页画布)")
            # 降级：整屏按几种候选缩放直接跑 vision，看得分
            for scale in (1.0, w / WIDTH if w < HEIGHT else HEIGHT / h, 0.5, 0.6, 0.75):
                if scale <= 0:
                    continue
                tw, th = int(WIDTH * scale), int(HEIGHT * scale)
                if tw > w or th > h:
                    continue
                crop = raw[:th, :tw] if tw <= w else raw
                obs = vision.analyze(cv2.resize(crop, (WIDTH, HEIGHT)), time.time())
                print(f"  fallback scale={scale:.3f} state={obs.state} top_scores={dict(list(obs.scores.items())[:6])}")
            return

        r = found.region
        print(f"LOCATE OK score={found.score:.3f} state={found.state} region={r.grab()}")
        crop = r.crop(raw)
        obs = vision.analyze(crop, time.time())
        print("STATE:", obs.state)
        print("control:", obs.control, "can_collect:", obs.can_collect,
              "x:", obs.x, "target:", obs.target, "bite_fraction:", obs.bite_fraction)
        scores = obs.scores
        top = sorted(((k, v) for k, v in scores.items() if isinstance(v, (int, float)) and k in (
            "title", "loot", "record", "congrats", "bar", "bite", "card_kg",
            "card_blue", "card_footer", "card_header", "green", "purple", "gray",
            "border", "flash_fraction", "bite_panel", "bite_soft")),
            key=lambda kv: -kv[1])
        print("scores:", json.dumps(dict(top), ensure_ascii=False))

        annotated = annotate(crop, obs)
        cv2.imencode(".png", annotated)[1].tofile(str(OUT / f"annotated_{ts}.png"))
        cv2.imencode(".png", crop)[1].tofile(str(OUT / f"crop_{ts}.png"))
        # 标注原图上的画布位置
        full = raw.copy()
        cv2.rectangle(full, (r.left, r.top), (r.left + r.width, r.top + r.height), (0, 255, 0), 4)
        cv2.putText(full, f"{found.state} score={found.score:.2f}", (r.left, max(40, r.top - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 0), 3)
        cv2.imencode(".png", full)[1].tofile(str(OUT / f"full_{ts}.png"))
        print("saved annotated:", OUT / f"annotated_{ts}.png", OUT / f"full_{ts}.png")
    finally:
        native.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
