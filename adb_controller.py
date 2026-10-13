# -*- coding: utf-8 -*-
"""
ADB 多实例管理器。
支持连接多个 MuMu 模拟器实例，每个实例独立连接、独立操作。
"""
import os
import subprocess
import shutil
import time


class ADBInstance:
    """单个模拟器实例的ADB连接"""

    def __init__(self, name, adb_path, host, port, role="main",
                 click_points=None, click_interval=1.0):
        self.name = name              # instance_1 / instance_2 / instance_3
        self.adb_path = adb_path
        self.host = host
        self.port = port
        self.role = role              # "main" 或 "worker"
        self.click_points = click_points or []
        self.click_interval = click_interval
        self.device_serial = f"{host}:{port}"
        self._connected = False

    def _run(self, *args, timeout=10):
        cmd = [self.adb_path, "-s", self.device_serial] + list(args)
        if not self._connected:
            cmd = [self.adb_path] + list(args)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "", "超时", -1
        except Exception as e:
            return "", str(e), -1

    def connect(self):
        out, err, _ = self._run("connect", self.device_serial)
        if "connected" in out or "already connected" in out:
            self._connected = True
            return True
        return False

    def is_connected(self):
        if not self._connected:
            return False
        out, _, _ = self._run("get-state")
        return "device" in out

    def ensure_connected(self):
        if self.is_connected():
            return True
        return self.connect()

    def tap(self, x, y):
        out, err, code = self._run("shell", "input", "tap", str(x), str(y))
        return code == 0

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        out, err, code = self._run(
            "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms))
        return code == 0

    def input_text(self, text):
        out, err, code = self._run("shell", "input", "text", text)
        return code == 0

    def screenshot(self, save_path="screenshot.png"):
        self._run("shell", "screencap", "-p", "/sdcard/screen.png")
        out, err, code = self._run("pull", "/sdcard/screen.png", save_path)
        return code == 0

    def key_event(self, keycode=4):
        out, err, code = self._run("shell", "input", "keyevent", str(keycode))
        return code == 0

    def execute_clicks(self):
        """按配置的 click_points 依次点击，返回是否成功"""
        if not self.click_points:
            return True
        for i, point in enumerate(self.click_points):
            x, y = point[0], point[1]
            self.tap(x, y)
            if i < len(self.click_points) - 1:
                time.sleep(self.click_interval)
        return True

    def status_dict(self):
        return {
            "name": self.name,
            "role": self.role,
            "host": self.host,
            "port": self.port,
            "connected": self.is_connected(),
        }


def find_mumu_adb(adb_path=None):
    """查找 MuMu 自带 adb"""
    if adb_path:
        return adb_path
    possible = [
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"),
                     "Netease", "MuMuPlayer-12.0", "shell", "adb.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
                     "Netease", "MuMuPlayer-12.0", "shell", "adb.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Programs", "Netease", "MuMuPlayer-12.0", "shell", "adb.exe"),
        "D:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\adb.exe",
        "E:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\adb.exe",
    ]
    for path in possible:
        if os.path.isfile(path):
            return path
    system_adb = shutil.which("adb")
    return system_adb or "adb"


class ADBManager:
    """管理多个模拟器实例"""

    def __init__(self, instances_config, adb_path=None):
        self.adb_path = find_mumu_adb(adb_path)
        self.instances = {}
        for name, cfg in instances_config.items():
            self.instances[name] = ADBInstance(
                name=name,
                adb_path=self.adb_path,
                host=cfg.get("host", "127.0.0.1"),
                port=cfg.get("port", 16384),
                role=cfg.get("role", "main"),
                click_points=cfg.get("click_points", []),
                click_interval=cfg.get("click_interval", 1.0),
            )

    def get_main_instances(self):
        """获取所有主号实例"""
        return {k: v for k, v in self.instances.items() if v.role == "main"}

    def get_worker_instances(self):
        """获取所有小号实例"""
        return {k: v for k, v in self.instances.items() if v.role == "worker"}

    def set_role(self, instance_name, role):
        """动态设置某个实例的角色
        :param role: "main" 或 "worker"
        """
        if instance_name not in self.instances:
            return False
        if role not in ("main", "worker"):
            return False
        self.instances[instance_name].role = role
        return True

    def set_roles(self, role_map):
        """批量设置角色 {instance_name: role}"""
        for name, role in role_map.items():
            self.set_role(name, role)

    def connect_all(self):
        """连接所有实例"""
        results = {}
        for name, inst in self.instances.items():
            results[name] = inst.ensure_connected()
        return results

    def get_status(self):
        """获取所有实例状态"""
        return {name: inst.status_dict() for name, inst in self.instances.items()}
