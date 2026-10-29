# -*- coding: utf-8 -*-
"""
MuMu模拟器自动发现（跨机器通用版）。
查找顺序（从高到低）：
  1. 环境变量 MUMU_ROOT（手动指定安装根目录）
  2. 项目目录下 mumu_path.txt（第一行写安装根目录）
  3. Windows 注册表卸载信息（DisplayName 含 MuMu）
  4. 常见安装路径（ProgramFiles / LOCALAPPDATA / 全盘扫描 Netease 目录）
实例发现：
  - 主路径：扫描 <root>/vms/*/configs/vm_config.json 拿每个实例的 ADB 端口
  - 兜底：MuMuManager.exe（官方 CLI）枚举实例
任何一步失败都会记录诊断信息（last_diagnosis），方便给用户可操作的提示。
"""
import os
import re
import json
import shutil
import string
import subprocess
from pathlib import Path

# MuMu 12 可能的目录名（国服 / 国际服）
MUMU_DIR_NAMES = ("MuMuPlayer-12.0", "MuMuPlayerGlobal-12.0")

# 根目录有效性：满足任意一条即可
def _valid_root(p: Path) -> bool:
    try:
        return (
            (p / "shell" / "adb.exe").is_file()
            or (p / "nx_main" / "MuMuManager.exe").is_file()
            or (p / "vms").is_dir()
        )
    except OSError:
        return False


# ── 诊断信息 ──
_last_diagnosis = []   # list[str]：最近一次 find_mumu_root 的尝试过程


def last_diagnosis():
    """返回最近一次根目录查找的诊断列表（给日志/前端展示用）"""
    return list(_last_diagnosis)


def _diag(msg):
    _last_diagnosis.append(msg)


# ── 1. 环境变量 ──
def _candidates_from_env():
    root = os.environ.get("MUMU_ROOT", "").strip().strip('"')
    if root:
        p = Path(root)
        _diag(f"环境变量 MUMU_ROOT={root}")
        yield p


# ── 2. mumu_path.txt ──
def _candidates_from_file():
    f = Path(__file__).resolve().parent / "mumu_path.txt"
    if not f.is_file():
        return
    try:
        line = f.read_text("utf-8-sig").strip().splitlines()
    except Exception as e:
        _diag(f"mumu_path.txt 读取失败: {e}")
        return
    if not line:
        return
    root = line[0].strip().strip('"')
    if not root:
        return
    _diag(f"mumu_path.txt 指定 {root}")
    yield Path(root)


# ── 3. 注册表 ──
def _candidates_from_registry():
    if os.name != "nt":
        return
    import winreg
    hives = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
    ]
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    seen = set()
    for hive, access in hives:
        try:
            key = winreg.OpenKey(hive, uninstall, 0, access)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                    i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(key, sub) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                except OSError:
                    continue
                if "mumu" not in str(name).lower():
                    continue
                # InstallLocation 优先
                loc = None
                for val in ("InstallLocation", "DisplayIcon", "UninstallString"):
                    try:
                        v, _ = winreg.QueryValueEx(sk, val)
                        v = str(v).split(",")[0].strip().strip('"')
                        if v and Path(v).exists():
                            loc = Path(v)
                            break
                    except OSError:
                        continue
                if not loc:
                    continue
                # 可能指向 exe（MuMuPlayer.exe）或安装目录本身
                p = loc if loc.is_dir() else loc.parent
                if p.name.lower() in ("shell", "nx_main"):
                    p = p.parent
                if str(p).lower() in seen:
                    continue
                seen.add(str(p).lower())
                _diag(f"注册表 [{name}] → {p}")
                yield p


# ── 4. 常见安装路径 + 全盘扫描 ──
def _all_drives():
    drives = []
    for letter in string.ascii_uppercase:
        p = Path(f"{letter}:\\")
        try:
            if p.exists():
                drives.append(letter)
        except OSError:
            continue
    return drives


def _candidates_from_paths():
    bases = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_key, "")
        if base:
            bases.append(Path(base))
            if env_key == "LOCALAPPDATA":
                bases.append(Path(base) / "Programs")
    drives = _all_drives()
    _diag(f"扫描盘符: {', '.join(drives) if drives else '无'}")
    for d in drives:
        root = Path(f"{d}:\\")
        bases.append(root / "Program Files")
        bases.append(root / "Program Files (x86)")
        bases.append(root)  # 直接装在盘根目录/自定义目录的情况
    for base in bases:
        for name in MUMU_DIR_NAMES:
            yield base / "Netease" / name
            yield base / name


def find_mumu_root(refresh=False):
    """自动查找MuMu 12安装根目录。找不到返回 None（诊断见 last_diagnosis()）"""
    global _last_diagnosis
    _last_diagnosis = []
    tried = set()
    sources = (_candidates_from_env(), _candidates_from_file(),
               _candidates_from_registry(), _candidates_from_paths())
    for source in sources:
        for p in source:
            key = str(p).lower()
            if key in tried:
                continue
            tried.add(key)
            if _valid_root(p):
                _diag(f"命中安装目录: {p}")
                return p
    _diag("所有候选路径均未命中（shell/adb.exe、nx_main/MuMuManager.exe、vms 三者至少要有一个）")
    return None


def find_mumu_adb():
    """查找MuMu自带的adb.exe；找不到退回系统 adb"""
    root = find_mumu_root()
    if root:
        adb = root / "shell" / "adb.exe"
        if adb.is_file():
            return str(adb)
    import shutil
    return shutil.which("adb") or "adb"


# ── 实例发现：主路径（vms 目录扫描）──
def _instances_from_vms(root: Path):
    instances = []
    vms_dir = root / "vms"
    if not vms_dir.is_dir():
        return instances
    for vm_name in sorted(vms_dir.iterdir()):
        if not vm_name.is_dir():
            continue
        config_dir = vm_name / "configs"
        vm_config = config_dir / "vm_config.json"
        if not vm_config.exists():
            continue
        try:
            settings = json.loads(vm_config.read_text("utf-8-sig"))
            port = settings["vm"]["nat"]["port_forward"]["adb"]["host_port"]

            customer_config = config_dir / "customer_config.json"
            keptlive = "false"
            if customer_config.exists():
                try:
                    cc = json.loads(customer_config.read_text("utf-8-sig"))
                    keptlive = str(cc.get("customer", {}).get("app_keptlive", "false"))
                except Exception:
                    pass

            index = 0
            parts = vm_name.name.split("-")
            if len(parts) > 1 and parts[-1].isdigit():
                index = int(parts[-1])

            instances.append({
                "index": index,
                "name": f"instance_{index + 1}",
                "serial": f"127.0.0.1:{port}",
                "port": port,
                "ipc_config": {
                    "root": str(root),
                    "vm": vm_name.name,
                    "instance": index,
                    "serial": f"127.0.0.1:{port}",
                },
                "root": str(root),
                "app_keptlive": keptlive,
                "vm_name": vm_name.name,
            })
        except Exception:
            continue
    return instances


# ── 实例发现：兜底（MuMuManager.exe 官方 CLI）──
_MUMU_MGR_CMDS = (["info", "-v", "all"], ["api", "-v", "all", "info"])


def _extract_json_objects(text):
    """从混合输出里抠出所有顶层 JSON 对象（MuMuManager 输出前面可能带日志行）"""
    out = []
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text):
        try:
            out.append(json.loads(m.group(0)))
        except Exception:
            continue
    return out


def _instances_from_manager(root: Path):
    mgr = root / "nx_main" / "MuMuManager.exe"
    if not mgr.is_file():
        mgr = root / "shell" / "MuMuManager.exe"
    if not mgr.is_file():
        _diag(f"MuMuManager.exe 不存在，无法兜底枚举实例: {root}")
        return []
    for args in _MUMU_MGR_CMDS:
        try:
            r = subprocess.run([str(mgr)] + args, capture_output=True, text=True,
                               timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            objs = _extract_json_objects((r.stdout or "") + "\n" + (r.stderr or ""))
        except Exception as e:
            _diag(f"MuMuManager {args[0]} 执行失败: {e}")
            continue
        found = []
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            index = obj.get("index")
            port = obj.get("adb_port") or obj.get("adb_port_ip")
            if index is None or port in (None, "", 0):
                continue
            try:
                index, port = int(index), int(port)
            except (TypeError, ValueError):
                continue
            found.append({"index": index, "port": port})
        if found:
            # 尝试把端口对回 vms 目录（有的话 IPC 也能用）
            vm_by_port = {}
            vms_dir = root / "vms"
            if vms_dir.is_dir():
                for vm in vms_dir.iterdir():
                    cfg = vm / "configs" / "vm_config.json"
                    if not cfg.is_file():
                        continue
                    try:
                        s = json.loads(cfg.read_text("utf-8-sig"))
                        vm_by_port[s["vm"]["nat"]["port_forward"]["adb"]["host_port"]] = vm.name
                    except Exception:
                        continue
            instances = []
            for it in sorted(found, key=lambda x: x["index"]):
                vm_name = vm_by_port.get(it["port"])
                ipc = None
                if vm_name:
                    ipc = {"root": str(root), "vm": vm_name,
                           "instance": it["index"],
                           "serial": f"127.0.0.1:{it['port']}"}
                instances.append({
                    "index": it["index"],
                    "name": f"instance_{it['index'] + 1}",
                    "serial": f"127.0.0.1:{it['port']}",
                    "port": it["port"],
                    "ipc_config": ipc,
                    "root": str(root),
                    "app_keptlive": "false",
                    "vm_name": vm_name or "",
                })
            _diag(f"MuMuManager 枚举到 {len(instances)} 个实例")
            return instances
    return []


def discover_instances():
    """
    自动发现所有MuMu实例。
    返回列表，每项包含: index, serial, port, ipc_config(dict|None), root
    """
    root = find_mumu_root()
    if not root:
        return []
    instances = _instances_from_vms(root)
    if instances:
        return instances
    _diag("vms 目录扫描未发现实例（可能实例未创建或 vm_config.json 缺失），尝试 MuMuManager 兜底")
    return _instances_from_manager(root)


def generate_ipc_json(instance, output_dir="."):
    """把IPC配置写成json文件（MuMuIO需要文件路径）"""
    path = Path(output_dir) / f"mumu-ipc-{instance['index']}.json"
    path.write_text(json.dumps(instance["ipc_config"], ensure_ascii=False, indent=2), "utf-8")
    return str(path)


def auto_connect_all(on_log=None):
    """
    自动发现所有MuMu实例并生成IPC配置文件。
    返回: {instance_name: {ipc_path, port, serial}} 或 {}
    """
    global _last_diagnosis
    log = on_log or (lambda s: None)
    instances = discover_instances()

    if not instances:
        diag = last_diagnosis()
        log("未发现MuMu模拟器实例。查找过程：")
        for d in diag:
            log(f"  · {d}")
        log("解决方法（任选其一）：")
        log("  1. 确认已安装 MuMu 12 并至少启动过一次，然后重试")
        log("  2. 在项目目录新建 mumu_path.txt，第一行写 MuMu 安装根目录，例如：")
        log("     D:\\Program Files\\Netease\\MuMuPlayer-12.0")
        log("  3. 或设置环境变量 MUMU_ROOT 指向安装根目录后重启服务")
        return {}

    log(f"发现 MuMu 安装目录: {instances[0]['root']}")
    log(f"发现 {len(instances)} 个MuMu实例")
    result = {}
    for inst in instances:
        if inst["app_keptlive"].lower() != "false":
            log(f"{inst['name']}: 警告：多应用保活未关闭，需要关闭才能后台截图")
            log(f"  （MuMu设置中心 → 其他 → 关闭「多应用保活」→ 重启该实例）")
        if inst["ipc_config"] is None:
            log(f"{inst['name']}: 端口{inst['port']}，ADB可用，但未找到实例目录，巡访引擎不可用")
            result[inst["name"]] = {
                "ipc_path": None,
                "port": inst["port"],
                "serial": inst["serial"],
            }
            continue
        ipc_path = generate_ipc_json(inst)
        result[inst["name"]] = {
            "ipc_path": ipc_path,
            "port": inst["port"],
            "serial": inst["serial"],
        }
        log(f"{inst['name']}: 端口{inst['port']}，IPC配置已生成")
    return result


# ── 命令行自检入口 ──
if __name__ == "__main__":
    root = find_mumu_root()
    print("=== MuMu 发现诊断 ===")
    for d in last_diagnosis():
        print(" ·", d)
    if root:
        print(f"\n安装根目录: {root}")
        insts = discover_instances()
        print(f"实例数: {len(insts)}")
        for it in insts:
            print(f"  {it['name']}  {it['serial']}  vm={it['vm_name'] or '?'}  "
                  f"保活={it['app_keptlive']}")
    else:
        print("\n未找到 MuMu 12 安装目录。")
        print("可在项目目录新建 mumu_path.txt（第一行写安装根目录），")
        print("或设置环境变量 MUMU_ROOT 后重试。")
