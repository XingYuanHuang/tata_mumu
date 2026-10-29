"""跨实例撒鱼饵节流门。

三个模拟器实例各自跑独立的 bridge 子进程，但共享同一个 data 目录。
任何实例点击"撒鱼饵"前必须先在此排队：读到上一次撒饵时间，
等到 last + gap 之后才放行并写入新时间戳，保证任意两次撒饵间隔 > 3 秒，
避免某一个号连续撒饵而其他号抢不到。

文件锁用 O_CREAT|O_EXCL 原子创建，进程崩溃后由 stale 检测自动清理。
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path


class BaitGate:
    def __init__(self, data_dir, gap=3.2, timeout=25.0, stale=60.0):
        self.dir = Path(data_dir)
        self.lock_path = self.dir / 'bait_gate.lock'
        self.time_path = self.dir / 'bait_last.json'
        self.gap = float(gap)
        self.timeout = float(timeout)
        self.stale = float(stale)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _last_click(self):
        try:
            return float(json.loads(self.time_path.read_text('utf-8'))['t'])
        except Exception:
            return 0.0

    def _clean_stale(self):
        try:
            if time.time() - self.lock_path.stat().st_mtime > self.stale:
                self.lock_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def wait_turn(self, cancel):
        """排队等到可以撒饵。返回 True=轮到本实例（并已登记本次时间戳），
        False=等待期间被取消或超时。调用方拿到 True 后应立即点击。"""
        deadline = time.monotonic() + self.timeout
        fd = None
        while fd is None:
            if cancel is not None and cancel.is_set():
                return False
            if time.monotonic() > deadline:
                return False
            self._clean_stale()
            try:
                fd = os.open(str(self.lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if cancel is not None:
                    cancel.wait(0.12)
                else:
                    time.sleep(0.12)
            except Exception:
                return False
        try:
            # 持锁等待：直到距上次撒饵超过 gap 才轮到本实例
            while True:
                wait = self._last_click() + self.gap - time.time()
                if wait <= 0:
                    break
                if cancel is not None and cancel.wait(min(wait, 0.2)):
                    return False
                if time.monotonic() > deadline:
                    return False
            try:
                self.time_path.write_text(
                    json.dumps({'t': time.time()}), encoding='utf-8')
            except Exception:
                pass
            return True
        finally:
            try:
                os.close(fd)
                self.lock_path.unlink()
            except Exception:
                pass
