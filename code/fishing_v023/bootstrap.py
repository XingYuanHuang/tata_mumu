"""Reuse an existing working environment before installing anything."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
CHECK="import sys,struct; assert (3,10)<=sys.version_info[:2]<=(3,13); assert struct.calcsize('P')==8; import cv2,numpy,mss,tkinter"

def works(exe):
    try:return subprocess.run([str(exe),'-c',CHECK],capture_output=True,timeout=20).returncode==0
    except (OSError,subprocess.TimeoutExpired):return False

def main():
    os.chdir(ROOT)
    print('\u6b63\u5728\u68c0\u67e5\u5df2\u5b89\u88c5\u7684 Python \u73af\u5883\uff0c\u8bf7\u7a0d\u7b49...',flush=True)
    if not (ROOT/'assets/title.png').exists():
        print('Extract the ENTIRE zip first, then double-click run.bat.');return 1
    candidates=[ROOT/'.venv/Scripts/python.exe',Path(sys.executable)]
    for parent in (ROOT.parent,ROOT.parent.parent):
        for name in ('tata_fishing_v011','tata_fishing_v01','tata_fishing_v020','tata_fishing_v021','tata_fishing_v022'):
            for rel in (name+'/.venv/Scripts/python.exe',name+'/'+name+'/.venv/Scripts/python.exe'):
                candidates.append(parent/rel)
    chosen=None;seen=set()
    for candidate in candidates:
        key=str(candidate).lower()
        if key in seen or not candidate.is_file():continue
        seen.add(key)
        if works(candidate):chosen=candidate;break
    if chosen is None:
        if not (3,10)<=sys.version_info[:2]<=(3,13):
            print('Please install Python 3.12 64-bit, including pip and Tcl/Tk.');return 1
        import struct
        if struct.calcsize('P')!=8:print('A 64-bit Python installation is required.');return 1
        print('Creating a local Python environment. Existing game files are not changed.')
        if subprocess.call([sys.executable,'-m','venv',str(ROOT/'.venv')]):return 1
        chosen=ROOT/'.venv/Scripts/python.exe'
        print('Installing dependencies. First installation needs internet; later launches do not.')
        command=[str(chosen),'-m','pip','install','--only-binary=:all:','--timeout','40','--retries','2','-r',str(ROOT/'requirements.txt')]
        try:result=subprocess.call(command)
        except OSError as exc:print(exc);return 1
        if result or not works(chosen):
            print('Dependency installation failed. Keep this window open and save the error.');return 1
    print('Python:',chosen)
    print('Starting TataFishing 0.2.3. F9 stops automation.')
    return subprocess.call([str(chosen),str(ROOT/'main.py')])

if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        import traceback;traceback.print_exc();sys.exit(1)
