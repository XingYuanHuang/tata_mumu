# -*- coding: utf-8 -*-
"""临时诊断：用线上 visit_one(scan_only=True) 跑完整小号流程验证。"""
import sys, os, time
sys.path.insert(0, 'code/fishing_v023')
from mumu_io import MuMuIO
from visit_engine import VisitEngine

UIDS = ["13054369", "14105303", "17988633"]
m = MuMuIO('mumu-ipc-1.json')
e = VisitEngine(m, assets_dir='visit_assets', on_log=lambda s: print('   ', s))

for uid in UIDS:
    print(f'===== 巡访 {uid} =====')
    t0 = time.time()
    r = e.visit_one(uid, scan_only=True)
    print(f'>>> 结果: {r}  耗时 {time.time()-t0:.1f}s')
    time.sleep(1.0)

m.close()
print('DONE')
