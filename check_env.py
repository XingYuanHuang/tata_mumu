# -*- coding: utf-8 -*-
"""
环境自检脚本（新机器先跑这个）。
只用 Python 标准库，不装任何依赖也能跑：

    python check_env.py

逐项检查：操作系统 → Python 版本 → 依赖包 → MuMu 安装目录 → adb →
模拟器实例端口连通性 → 多应用保活 → 原生截图 SDK。
每项失败都给出具体修复方法。
"""
import os
import sys
import json
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
OK, WARN, FAIL = "[OK]  ", "[警告]", "[失败]"
problems = []      # 阻断性问题
warnings = []      # 不阻断但要提醒


def mark(level, msg):
    print(f"  {level} {msg}")
    if level == FAIL:
        problems.append(msg)
    elif level == WARN:
        warnings.append(msg)


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), -1


def main():
    print("=" * 56)
    print("巡访助手 · 环境自检")
    print("=" * 56)

    # ── 1. 操作系统 ──
    print("\n[1/6] 操作系统")
    if os.name != "nt":
        mark(FAIL, f"当前系统不是 Windows（{sys.platform}），本项目仅支持 Windows")
    else:
        mark(OK, f"Windows（{sys.getwindowsversion()}）")

    # ── 2. Python 版本 ──
    print("\n[2/6] Python")
    v = sys.version_info
    if v < (3, 9):
        mark(FAIL, f"Python 版本过旧 {v.major}.{v.minor}，建议 3.10+")
    else:
        mark(OK, f"Python {v.major}.{v.minor}.{v.micro}")

    # ── 3. 依赖包 ──
    print("\n[3/6] Python 依赖包")
    required = ["fastapi", "uvicorn", "websockets", "psutil", "uiautomation",
                "openpyxl", "cv2", "numpy"]
    missing = []

    # 优先用项目自带的 .venv 检查（服务实际就是用它跑的）
    venv_py = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    using_venv = False
    if venv_py.is_file():
        if Path(sys.executable).resolve() != venv_py.resolve():
            mark(WARN, f"当前解释器不是项目自带的 .venv，改用 .venv 检查依赖")
        using_venv = True

    def _has_module(mod):
        if using_venv:
            _, _, code = run([str(venv_py), "-c", f"import {mod}"], timeout=30)
            return code == 0
        try:
            __import__(mod)
            return True
        except ImportError:
            return False

    for mod in required:
        if _has_module(mod):
            mark(OK, mod + ("（.venv）" if using_venv else ""))
        else:
            mark(FAIL, f"缺少 {mod}")
            missing.append(mod)
    if missing:
        target = ".venv\\Scripts\\python.exe" if using_venv else "python"
        print(f"\n        一键安装（国内镜像）：")
        print(f"        {target} -m pip install "
              f"-i https://pypi.tuna.tsinghua.edu.cn/simple {' '.join(missing)}")

    # ── 4. MuMu 安装目录 & adb ──
    print("\n[4/6] MuMu 模拟器")
    sys.path.insert(0, str(PROJECT_DIR))
    from mumu_discover import find_mumu_root, last_diagnosis, discover_instances
    root = find_mumu_root()
    if not root:
        mark(FAIL, "未找到 MuMu 12 安装目录")
        print("\n        查找过程：")
        for d in last_diagnosis():
            print(f"          · {d}")
        print("""
        修复方法（任选其一）：
          a. 确认已安装 MuMu 12 并至少完整启动过一次
          b. 在项目目录新建 mumu_path.txt，第一行写安装根目录，例如：
               D:\\Program Files\\Netease\\MuMuPlayer-12.0
          c. 或设置环境变量 MUMU_ROOT 指向安装根目录后重跑本脚本
        注意：根目录下必须能看到 shell、vms 或 nx_main 之一""")
    else:
        mark(OK, f"安装目录: {root}")
        adb = root / "shell" / "adb.exe"
        if adb.is_file():
            mark(OK, f"adb.exe: {adb}")
        else:
            mark(WARN, "根目录下没有 shell/adb.exe，将回退使用系统 adb")

    # ── 5. 模拟器实例 ──
    print("\n[5/6] 模拟器实例")
    if not root:
        mark(WARN, "未找到安装目录，跳过实例检测")
    else:
        instances = discover_instances()
        if not instances:
            mark(FAIL, "没有发现任何模拟器实例（vms 目录为空或 vm_config.json 缺失）")
            print("        请先启动 MuMu 12 创建/启动至少一个实例，再重跑本脚本")
        else:
            for inst in instances:
                name = inst["name"]
                serial = inst["serial"]
                # ADB 连通性
                run([str(adb), "connect", serial])
                state, _, _ = run([str(adb), "-s", serial, "get-state"])
                if "device" in state:
                    mark(OK, f"{name}: ADB {serial} 已连接")
                else:
                    mark(FAIL, f"{name}: ADB {serial} 连不上（请确认该实例已启动，"
                               f"或 MuMu 设置中心 → ADB 调试已开启）")
                # 保活
                if str(inst.get("app_keptlive", "false")).lower() != "false":
                    mark(WARN, f"{name}: 多应用保活未关闭，后台截图会黑屏/失败；"
                               f"请在 MuMu 设置中心 → 其他 关闭后重启该实例")
                else:
                    mark(OK, f"{name}: 多应用保活已关闭")
                # IPC / SDK
                if inst.get("ipc_config"):
                    dlls = [root / "nx_device/12.0/shell/sdk/external_renderer_ipc.dll",
                            root / "shell/sdk/external_renderer_ipc.dll",
                            root / "nx_main/sdk/external_renderer_ipc.dll"]
                    if any(d.is_file() for d in dlls):
                        mark(OK, f"{name}: 原生截图 SDK 存在（IPC 可用）")
                    else:
                        mark(WARN, f"{name}: 未找到 external_renderer_ipc.dll，"
                                   f"巡访引擎（后台截图）不可用")
                else:
                    mark(WARN, f"{name}: 未找到实例目录，巡访引擎不可用（仅 ADB 点击可用）")

    # ── 6. 项目文件 ──
    print("\n[6/6] 项目文件")
    for f, tip in [("requirements.txt", "依赖清单"),
                   ("uid_list.xlsx", "小号 UID 名单"),
                   ("config.py", "全局配置")]:
        if (PROJECT_DIR / f).is_file():
            mark(OK, f"{f}（{tip}）")
        elif f == "uid_list.xlsx":
            mark(WARN, f"{f} 不存在——小号通道需要它，不跑小号可忽略")
        else:
            mark(FAIL, f"{f} 缺失（{tip}），项目文件不完整")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if problems:
        print(f"自检未通过：{len(problems)} 个阻断问题，按上面提示修复后重跑。")
        print("全部修复后：双击 启动服务.bat，浏览器打开 http://localhost:8000，")
        print("点顶部「自动检测环境」确认绿灯后即可使用。")
        return 1
    print("自检通过！双击 启动服务.bat（或 python server.py）即可使用。")
    if warnings:
        print(f"另有 {len(warnings)} 条警告，建议处理（不影响启动）。")
    print("使用指引见：环境设置指引.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
