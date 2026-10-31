# -*- coding: utf-8 -*-
"""配置文件"""

# ── 消息监控配置 ──
POLL_INTERVAL = 1.0
DEDUP_CACHE_SIZE = 200

# ── QQ 监控的群（可选）──
# 填群名（填窗口标题的一部分即可），会优先监控该群的窗口。
# 留 None 时自动挑第一个打开的 QQ 聊天窗口。
QQ_TARGET_GROUP = "塔塔夺金MVP"

# ── UID位数配置 ──
UID_LENGTH = 8  # 游戏UID位数，按实际改

# ── MuMu 多实例 ADB 配置 ──
# MuMu 12 多开的 ADB 端口按 **+32** 递增（不是 +1）：
#   实例1 = 16384, 实例2 = 16416, 实例3 = 16448 ...
# 可用 MuMu 安装目录下的 shell\adb.exe devices 核对实际端口。
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
        "port": 16416,
        "role": "main",
        "click_points": [],
        "click_interval": 1.0,
        "ipc_config": None,
    },
    # 说明：这里写死的只是「角色偏好」。服务启动/点“自动检测环境”时，
    # 会自动扫描 MuMu 的所有实例，把没写在这里的实例也登记进来
    # （新增实例默认角色=小号 worker，可在面板下拉里改成主号）。
    # 所以开了第 3、第 4 个模拟器不需要改这个文件。
}

ADB_PATH = None
# 留 None = 自动探测 MuMu 自带的 adb（兼容 shell\ / nx_main\ / nx_device\ 三种布局，
# 并遍历所有盘符找安装目录），换机器不用改这里。
# 自动探测失败时才手动指定，例如：
# ADB_PATH = r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe"

# ── 小号 Excel UID 配置 ──
EXCEL_CONFIG = {
    "file_path": "uid_list.xlsx",
    # 不依赖表头/列名：自动扫描整张表所有单元格，提取所有 UID 位数（UID_LENGTH）
    # 的数字并去重
    "poll_interval": 5.0,
    # 处理完在表右侧写「processed」列标记（仅用于跨重启跳过，不参与 UID 识别）
    "mark_processed": True,
    "processed_column": "processed",
}

# ── 服务配置 ──
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
