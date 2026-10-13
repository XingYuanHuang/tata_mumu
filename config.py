# -*- coding: utf-8 -*-
"""配置文件"""

# ── 消息监控配置 ──
POLL_INTERVAL = 1.0
DEDUP_CACHE_SIZE = 200

# ── UID位数配置 ──
UID_LENGTH = 8  # 游戏UID位数，按实际改

# ── MuMu 多实例 ADB 配置 ──
# MuMu 12 多开端口递增：16384, 16385, 16386
ADB_INSTANCES = {
    "instance_1": {
        "host": "127.0.0.1",
        "port": 16384,
        "role": "main",
        "click_points": [],
        "click_interval": 1.0,
        # MuMu原生SDK的IPC配置文件路径（用于巡访引擎后台截图）
        # 在MuMu安装目录下找 mumu-ipc.json，或用MuMuManager导出
        "ipc_config": None,  # "C:\\path\\to\\mumu-ipc-0.json"
    },
    "instance_2": {
        "host": "127.0.0.1",
        "port": 16385,
        "role": "main",
        "click_points": [],
        "click_interval": 1.0,
        "ipc_config": None,
    },
    "instance_3": {
        "host": "127.0.0.1",
        "port": 16386,
        "role": "worker",
        "click_points": [],
        "click_interval": 1.0,
        "ipc_config": None,
    },
}

ADB_PATH = None

# ── 小号 Excel UID 配置 ──
EXCEL_CONFIG = {
    "file_path": "uid_list.xlsx",
    "uid_column": "uid",
    "poll_interval": 5.0,
    "mark_processed": True,
    "processed_column": "processed",
}

# ── 服务配置 ──
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
