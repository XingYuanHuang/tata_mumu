# -*- coding: utf-8 -*-
"""集成测试：FishingManager 启动/暂停/继续/停止/状态/纪录（实例3，2竿）。"""
import sys
import time
sys.stdout.reconfigure(encoding="utf-8")
from fishing_manager import FishingManager

logs = []
records = []
mgr = FishingManager(on_log=logs.append,
                     on_record=lambda rec: records.append(rec))

print("available:", mgr.available_instances())

r = mgr.start("instance_3", limit=2, miss_min=20, miss_max=30)
print("start:", r)
assert r["ok"], r

time.sleep(8)
print("status@8s:", mgr.status()["instance_3"])

# 暂停 3 秒
print("pause:", mgr.pause("instance_3"))
time.sleep(3)
print("status@paused:", mgr.status()["instance_3"]["paused"])
print("resume:", mgr.resume("instance_3"))

# 等待结束（最多 120 秒）
deadline = time.time() + 120
while time.time() < deadline:
    st = mgr.status()["instance_3"]
    if not st["running"]:
        break
    time.sleep(2)
print("final status:", mgr.status()["instance_3"])
print("records:", {k: len(v) for k, v in mgr.records_status().items()})
print("recent logs:")
for line in logs[-8:]:
    print("  ", line)
mgr.stop_all()
print("DONE")
