# -*- coding: utf-8 -*-
"""钓鱼多实例管理器。

为每个模拟器实例（instance_1/2/3 → mumu-ipc-{0,1,2}.json）拉起一个
fishing_v023/bridge.py 子进程，通过 stdin 发 stop/pause/resume 命令，
解析 stdout 的 JSON 事件流更新状态；出现"刷新纪录卡"时保存带截图的纪录。
"""
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FISH_DIR = ROOT / "code" / "fishing_v023"
RECORDS_FILE = ROOT / "fishing_records.json"
DATA_DIR = ROOT / "debug_fishing" / "data"
MAX_RECORDS_PER_INSTANCE = 30
# 纪录卡去重时间窗（秒）：结算画面闪烁会在 1~3 秒内重复上报同一张卡
RECORD_DEDUP_SECONDS = 6


def _parse_record_time(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


class FishingWorker:
    """一个模拟器实例对应的钓鱼子进程。"""

    def __init__(self, name, ipc_config, handle, on_log, on_record):
        self.name = name
        self.ipc_config = str(ipc_config)
        self.handle = handle          # MuMuIO handle = 实例序号 + 1
        self.on_log = on_log
        self.on_record = on_record
        self.proc = None
        self.status = self._fresh_status()

    @staticmethod
    def _fresh_status():
        return dict(running=False, paused=False, instance="", state="", stop_reason="",
                    casts=0, qte=0, collected=0, hooks=0, casts_per_minute=0.0,
                    bait=False, level=None, bait_count=None)

    def start(self, limit=30, miss_min=20, miss_max=30, latency=35, bait=False):
        if self.proc and self.proc.poll() is None:
            return False, "已在运行"
        miss_min = max(1, int(miss_min))
        miss_max = max(miss_min, int(miss_max))
        env = dict(os.environ)
        env["FISH_MISS_MIN"] = str(miss_min)
        env["FISH_MISS_MAX"] = str(miss_max)
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [sys.executable, str(FISH_DIR / "bridge.py"), "--run",
               "--ipc-config", self.ipc_config, "--data", str(DATA_DIR)]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env, cwd=str(FISH_DIR),
                text=True, encoding="utf-8", bufsize=1)
        except Exception as e:
            return False, f"启动失败: {e}"
        self.status = self._fresh_status()
        self.status.update(running=True, paused=False, instance=self.name,
                           bait=bool(bait))
        # 先标记 running 再发首行命令，避免 reader 线程抢先置 False
        if not self._write(dict(handle=self.handle, mode="auto",
                                limit=int(limit), latency=float(latency),
                                bait=bool(bait))):
            self.stop("stdin 不可用")
            return False, "命令发送失败"
        threading.Thread(target=self._reader, daemon=True).start()
        self.on_log(f"[钓鱼] {self.name} 已启动：上限 {limit} 竿，"
                    f"QTE 失误间隔 {miss_min}~{miss_max} 次，"
                    f"撒鱼饵{'开' if bait else '关'}")
        return True, ""

    def set_bait(self, on):
        """运行中随时切换撒鱼饵模式。"""
        on = bool(on)
        if self.proc and self.proc.poll() is None:
            if not self._write(dict(command="bait", on=on)):
                return False
        self.status["bait"] = on
        return True

    def _write(self, obj):
        try:
            if self.proc and self.proc.stdin:
                self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                return True
        except Exception as e:
            self.on_log(f"[钓鱼] {self.name} 命令发送失败: {e}")
        return False

    def pause(self):
        if self._write(dict(command="pause")):
            self.status["paused"] = True
            return True
        return False

    def resume(self):
        if self._write(dict(command="resume")):
            self.status["paused"] = False
            return True
        return False

    def stop(self, reason="手动停止"):
        if self.proc and self.proc.poll() is None:
            self._write(dict(command="stop"))
            try:
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            self.on_log(f"[钓鱼] {self.name} 已停止（{reason}）")
        self.status["running"] = False
        self.status["paused"] = False

    def _reader(self):
        proc = self.proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue  # 子进程偶发的非 JSON 输出
                kind, val = msg.get("kind"), msg.get("value")
                if kind == "stats" and isinstance(val, dict):
                    self.status.update(
                        casts=val.get("casts", 0), qte=val.get("qte", 0),
                        collected=val.get("collected", 0),
                        casts_per_minute=val.get("casts_per_minute", 0.0))
                    if val.get("level") is not None:
                        self.status["level"] = val["level"]
                    if val.get("bait") is not None:
                        self.status["bait_count"] = val["bait"]
                elif kind == "status" and isinstance(val, str):
                    self.status["state"] = val
                elif kind == "hook_stats" and isinstance(val, dict):
                    self.status["hooks"] = val.get("hooks", 0)
                elif kind == "record" and isinstance(val, dict):
                    self.on_record(self.name, val)
                elif kind == "finished":
                    self.status.update(running=False, paused=False, stop_reason=val)
                    self.on_log(f"[钓鱼] {self.name} 结束: {val}")
                elif kind == "detail":
                    self.on_log(f"[钓鱼] {self.name}: {val}")
                # 'log' 行太详细，不入面板日志
        except Exception as e:
            self.on_log(f"[钓鱼] {self.name} 输出读取异常: {e}")
        finally:
            self.status["running"] = False
            self.status["paused"] = False


class FishingManager:
    """按实例名管理 worker，并持久化各实例的刷新纪录。"""

    def __init__(self, on_log, on_record):
        self.on_log = on_log
        self.on_record = on_record
        self.workers = {}
        self.records = self._load_records()

    # ── 纪录持久化 ──
    def _load_records(self):
        try:
            data = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
        # 历史数据自愈：清理时间窗内的重复记录（旧版本闪烁重复入库）
        for name, lst in list(data.items()):
            cleaned = []
            for rec in lst:  # 列表按新→旧排列
                t = _parse_record_time(rec.get("time", ""))
                if t and cleaned:
                    t_prev = _parse_record_time(cleaned[-1].get("time", ""))
                    if t_prev and abs((t_prev - t).total_seconds()) < RECORD_DEDUP_SECONDS:
                        continue
                cleaned.append(rec)
            data[name] = cleaned
        return data

    def _save_records(self):
        try:
            RECORDS_FILE.write_text(
                json.dumps(self.records, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.on_log(f"[钓鱼] 纪录保存失败: {e}")

    def _handle_record(self, name, rec):
        rec = dict(rec)
        rec["instance"] = name
        lst = self.records.setdefault(name, [])
        # 结算画面会在"纪录卡↔未知画面"间闪烁几秒，导致同一张卡重复上报。
        # 时间窗去重：与上一条间隔 < 6 秒视为同一张卡（真实连续纪录至少隔一整竿）。
        t_new = _parse_record_time(rec.get("time", ""))
        if t_new and lst:
            t_prev = _parse_record_time(lst[0].get("time", ""))
            if t_prev and abs((t_new - t_prev).total_seconds()) < RECORD_DEDUP_SECONDS:
                return
        if lst and lst[0].get("time") == rec.get("time"):
            return  # 同一秒去重
        lst.insert(0, rec)
        del lst[MAX_RECORDS_PER_INSTANCE:]
        self._save_records()
        self.on_record(rec)
        self.on_log(f"[钓鱼] {name} 🏆 刷新纪录！已记录（共 {len(lst)} 条）")

    # ── worker 管理 ──
    def _worker(self, name):
        w = self.workers.get(name)
        if w:
            return w
        m = re.search(r"(\d+)$", name or "")
        if not m:
            raise ValueError(f"无法从实例名解析序号: {name}")
        index = int(m.group(1)) - 1
        ipc = ROOT / f"mumu-ipc-{index}.json"
        if not ipc.is_file():
            raise FileNotFoundError(
                f"{ipc.name} 不存在，请先在面板点「自动检测环境」生成 IPC 配置")
        w = FishingWorker(name, ipc, index + 1, self.on_log, self._handle_record)
        self.workers[name] = w
        return w

    def start(self, name, limit=30, miss_min=20, miss_max=30, latency=35, bait=False):
        try:
            w = self._worker(name)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        ok, err = w.start(limit, miss_min, miss_max, latency, bait)
        return {"ok": ok, **({"error": err} if err else {})}

    def set_bait(self, name, on):
        w = self.workers.get(name)
        if not w:
            # 未启动过的实例也可记住开关状态，下次启动生效
            try:
                w = self._worker(name)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        if not w.status["running"]:
            # 未运行也记住开关状态，下次启动生效
            w.status["bait"] = bool(on)
            return {"ok": True, "deferred": True}
        return {"ok": w.set_bait(on)}

    def pause(self, name):
        w = self.workers.get(name)
        if not w or not w.status["running"]:
            return {"ok": False, "error": "该实例未在运行"}
        return {"ok": w.pause()}

    def resume(self, name):
        w = self.workers.get(name)
        if not w or not w.status["running"]:
            return {"ok": False, "error": "该实例未在运行"}
        return {"ok": w.resume()}

    def stop(self, name=None):
        names = [name] if name else list(self.workers)
        for n in names:
            w = self.workers.get(n)
            if w:
                w.stop()
        return {"ok": True}

    def stop_all(self):
        for w in self.workers.values():
            try:
                w.stop("服务关闭")
            except Exception:
                pass

    def status(self):
        return {name: dict(w.status) for name, w in self.workers.items()}

    def available_instances(self):
        """当前有 IPC 配置、可以启动钓鱼的实例名列表。"""
        names = []
        for p in sorted(ROOT.glob("mumu-ipc-*.json")):
            m = re.search(r"mumu-ipc-(\d+)\.json$", p.name)
            if m:
                names.append(f"instance_{int(m.group(1)) + 1}")
        return names

    def records_status(self, with_image=False):
        out = {}
        for name, lst in self.records.items():
            out[name] = [r if with_image else {k: v for k, v in r.items() if k != "image"}
                         for r in lst]
        return out

    def clear_records(self, name=None):
        if name:
            n = len(self.records.get(name, []))
            self.records.pop(name, None)
        else:
            n = sum(len(v) for v in self.records.values())
            self.records = {}
        self._save_records()
        return n
