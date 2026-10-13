"""Local JSON bridge to the original v023 worker with unified stop controls."""
import argparse
import json
import math
from pathlib import Path
import sys
import threading
import traceback

ROOT=Path(__file__).resolve().parent
output_lock=threading.Lock()

def emit(kind,value):
    if kind=='preview':return
    if kind=='finished':
        from zh import tr
        value=tr(value)
    with output_lock:
        print(json.dumps(dict(kind=kind,value=value),ensure_ascii=False),flush=True)

def validate(q,windows):
    hwnd=int(q['handle'])
    if hwnd not in {int(h) for h,_ in windows}:raise ValueError('所选窗口已关闭或不是 MuMu')
    mode=q.get('mode','single');limit=int(q.get('limit',3));latency=float(q.get('latency',35))
    if mode not in ('single','auto','observe') or not 1<=limit<=999 or not math.isfinite(latency) or not 0<=latency<=180:raise ValueError('钓鱼参数无效')
    return hwnd,mode,limit,latency

def run(q,native,data):
    from runtime import Runner,Session,Settings
    hwnd,mode,limit,latency=validate(q,native.candidates())
    if native.old_helpers():raise RuntimeError('请先停止旧版钓鱼助手')
    if not native.single_instance():raise RuntimeError('已有钓鱼助手运行，请先关闭它')
    cancel=threading.Event()
    def control():
        try:
            for line in sys.stdin:
                if json.loads(line).get('command')=='stop':cancel.set();return
        finally:cancel.set()
    def hotkeys():
        while not cancel.wait(.02):
            if native.key(0x77) or native.key(0x78):cancel.set();return
    threading.Thread(target=control,daemon=True).start()
    threading.Thread(target=hotkeys,daemon=True).start()
    backend='mumu-ipc' if getattr(native,'background',False) else q.get('backend','sendinput')
    settings=Settings(mode,limit,latency,False,backend,q.get('edge','both'))
    session=Session(data,emit)
    try:Runner(native,ROOT/'assets',session,settings,hwnd,cancel,emit).run()
    finally:cancel.set();native.release()

def main():
    p=argparse.ArgumentParser();p.add_argument('--windows',action='store_true');p.add_argument('--run',action='store_true');p.add_argument('--data',type=Path,default=ROOT/'data');p.add_argument('--ipc-config',type=Path,required=True);args=p.parse_args()
    from mumu_io import MuMuIO
    native=MuMuIO(args.ipc_config)
    try:
        if args.windows:
            print(json.dumps([dict(handle=h,title=t) for h,t in native.candidates()],ensure_ascii=False),flush=True);return
        if not args.run:raise ValueError('missing bridge operation')
        first=sys.stdin.readline()
        if first:run(json.loads(first),native,args.data)
    finally:native.close()

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:main()
    except Exception as exc:emit('detail',str(exc));traceback.print_exc(file=sys.stderr);sys.exit(1)
