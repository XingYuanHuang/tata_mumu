# -*- coding: utf-8 -*-
"""
MuMu模拟器自动发现。
扫描MuMu安装目录，自动发现所有实例，生成IPC配置，不需要手动配置。
"""
import os
import json
import subprocess
from pathlib import Path

# MuMu 12 的 adb 在三处之一，取决于版本/安装方式（换机器最容易在这踩坑）
ADB_RELS = ("shell/adb.exe", "nx_main/adb.exe", "nx_device/12.0/shell/adb.exe")


def find_mumu_root():
    """自动查找MuMu 12安装目录"""
    candidates = []
    # 从环境变量找
    for env_key in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]:
        base = os.environ.get(env_key, "")
        if base:
            candidates.append(Path(base) / "Netease" / "MuMuPlayer-12.0")
            candidates.append(Path(base) / "Netease" / "MuMuPlayerGlobal-12.0")
    
    # 常见安装路径（遍历所有存在的盘符，换机器不用改代码）
    for drive in "CDEFGH":
        if not Path(f"{drive}:\\").exists():
            continue
        for sub in ("Program Files", "Program Files (x86)"):
            candidates.append(Path(f"{drive}:\\{sub}\\Netease\\MuMuPlayer-12.0"))
            candidates.append(Path(f"{drive}:\\{sub}\\Netease\\MuMuPlayerGlobal-12.0"))
    
    for p in candidates:
        if not p.is_dir():
            continue
        if any((p / rel).is_file() for rel in ADB_RELS):
            return p

    # 再退一步：vms 目录 + nx_main 都在，也算有效安装（adb 位置以后可能再变）
    for p in candidates:
        if p.is_dir() and (p / "vms").is_dir() and (p / "nx_main").is_dir():
            return p

    return None


def find_mumu_adb():
    """查找MuMu自带的adb.exe（兼容 shell/ 与 nx_main/ 两种布局）"""
    root = find_mumu_root()
    if root:
        for rel in ADB_RELS:
            adb = root / rel
            if adb.is_file():
                return str(adb)
    # 退回统一入口（还包含环境变量/系统 PATH 兜底）
    try:
        from adb_controller import find_mumu_adb as _resolve
        return _resolve(None)
    except Exception:
        import shutil
        return shutil.which("adb") or "adb"


def discover_instances():
    """
    自动发现所有MuMu实例。
    返回列表，每项包含: index, serial, port, ipc_config(dict), root
    """
    root = find_mumu_root()
    if not root:
        return []
    
    instances = []
    vms_dir = root / "vms"
    if not vms_dir.is_dir():
        return []
    
    # 扫描vms目录下的实例
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
            
            # 读取customer_config检查保活
            customer_config = config_dir / "customer_config.json"
            keptlive = "false"
            if customer_config.exists():
                cc = json.loads(customer_config.read_text("utf-8-sig"))
                keptlive = str(cc.get("customer", {}).get("app_keptlive", "false"))
            
            # 从目录名提取实例索引（如 vmd-0 → 0）
            index = 0
            parts = vm_name.name.split("-")
            if len(parts) > 1 and parts[-1].isdigit():
                index = int(parts[-1])
            
            # 构造IPC配置（直接生成dict，不需要写文件）
            ipc_config = {
                "root": str(root),
                "vm": vm_name.name,
                "instance": index,
                "serial": f"127.0.0.1:{port}",
            }
            
            instances.append({
                "index": index,
                "name": f"instance_{index + 1}",
                "serial": f"127.0.0.1:{port}",
                "port": port,
                "ipc_config": ipc_config,
                "root": str(root),
                "app_keptlive": keptlive,
                "vm_name": vm_name.name,
            })
        except Exception:
            continue
    
    return instances


def generate_ipc_json(instance, output_dir="."):
    """把IPC配置写成json文件（MuMuIO需要文件路径）"""
    path = Path(output_dir) / f"mumu-ipc-{instance['index']}.json"
    path.write_text(json.dumps(instance["ipc_config"], ensure_ascii=False, indent=2), "utf-8")
    return str(path)


def auto_connect_all(on_log=None):
    """
    自动发现所有MuMu实例并生成IPC配置文件。
    返回: {instance_name: ipc_config_path} 或 {} 
    """
    log = on_log or (lambda s: None)
    instances = discover_instances()
    
    if not instances:
        log("未发现MuMu实例，请确保MuMu 12已安装并启动")
        return {}
    
    log(f"发现 {len(instances)} 个MuMu实例")
    result = {}
    for inst in instances:
        # 检查保活
        if inst["app_keptlive"].lower() != "false":
            log(f"{inst['name']}: 警告：多应用保活未关闭，需要关闭才能后台截图")
        
        # 生成IPC配置文件
        ipc_path = generate_ipc_json(inst)
        result[inst["name"]] = {
            "ipc_path": ipc_path,
            "port": inst["port"],
            "serial": inst["serial"],
        }
        log(f"{inst['name']}: 端口{inst['port']}，IPC配置已生成")
    
    return result
