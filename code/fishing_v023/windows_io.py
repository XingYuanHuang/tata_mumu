"""Documented foreground Windows input; no drivers, hooks or detection bypass.

OS input acceptance is not game acceptance. The controller confirms visual changes.
All Win32 structures use fixed-width fields even in offline Linux tests.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
import sys
import threading
import time

DWORD=C.c_uint32
LONG=C.c_int32
WORD=C.c_uint16
ULONG_PTR=C.c_size_t
class MOUSEINPUT(C.Structure):
    _fields_=[('dx',LONG),('dy',LONG),('mouseData',DWORD),('dwFlags',DWORD),
              ('time',DWORD),('dwExtraInfo',ULONG_PTR)]
class KEYBDINPUT(C.Structure):
    _fields_=[('wVk',WORD),('wScan',WORD),('dwFlags',DWORD),('time',DWORD),('dwExtraInfo',ULONG_PTR)]
class HARDWAREINPUT(C.Structure):
    _fields_=[('uMsg',DWORD),('wParamL',WORD),('wParamH',WORD)]
class INPUTUNION(C.Union):
    _fields_=[('mi',MOUSEINPUT),('ki',KEYBDINPUT),('hi',HARDWAREINPUT)]
class INPUT(C.Structure):
    _anonymous_=('data',)
    _fields_=[('type',DWORD),('data',INPUTUNION)]

@dataclass
class Receipt:
    requested: tuple[int,int]
    actual: tuple[int,int]
    backend: str
    down_at: float
    up_at: float
    move_ms: float
    @property
    def hold_ms(self):return (self.up_at-self.down_at)*1000

class WindowsIO:
    def __init__(self):
        if sys.platform!='win32':raise RuntimeError('Live input requires Windows.')
        self.u=C.WinDLL('user32',use_last_error=True)
        self.k=C.WinDLL('kernel32',use_last_error=True)
        self.a=C.WinDLL('advapi32',use_last_error=True)
        try:
            self.u.SetProcessDpiAwarenessContext.argtypes=[W.HANDLE]
            self.u.SetProcessDpiAwarenessContext.restype=W.BOOL
            self.u.SetProcessDpiAwarenessContext(C.c_void_p(-4))
        except (AttributeError,OSError):
            try:C.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError,OSError):self.u.SetProcessDPIAware()
        specs={
            'GetForegroundWindow':([],W.HWND),
            'SetForegroundWindow':([W.HWND],W.BOOL),
            'GetAncestor':([W.HWND,W.UINT],W.HWND),
            'WindowFromPoint':([W.POINT],W.HWND),
            'GetCursorPos':([C.POINTER(W.POINT)],W.BOOL),
            'SetCursorPos':([C.c_int,C.c_int],W.BOOL),
            'GetWindowRect':([W.HWND,C.POINTER(W.RECT)],W.BOOL),
            'GetWindowTextW':([W.HWND,W.LPWSTR,C.c_int],C.c_int),
            'IsWindow':([W.HWND],W.BOOL),
            'IsWindowVisible':([W.HWND],W.BOOL),
            'IsIconic':([W.HWND],W.BOOL),
            'GetAsyncKeyState':([C.c_int],C.c_short),
            'SendInput':([W.UINT,C.POINTER(INPUT),C.c_int],W.UINT),
            'GetWindowThreadProcessId':([W.HWND,C.POINTER(DWORD)],DWORD),
            'mouse_event':([DWORD,DWORD,DWORD,DWORD,ULONG_PTR],None),
        }
        for name,(args,ret) in specs.items():
            f=getattr(self.u,name);f.argtypes=args;f.restype=ret
        self.k.OpenProcess.argtypes=[DWORD,W.BOOL,DWORD];self.k.OpenProcess.restype=W.HANDLE
        self.k.CloseHandle.argtypes=[W.HANDLE];self.k.CloseHandle.restype=W.BOOL
        self.k.GetCurrentProcess.restype=W.HANDLE
        self.k.CreateMutexW.argtypes=[W.LPVOID,W.BOOL,W.LPCWSTR];self.k.CreateMutexW.restype=W.HANDLE
        self.a.OpenProcessToken.argtypes=[W.HANDLE,DWORD,C.POINTER(W.HANDLE)];self.a.OpenProcessToken.restype=W.BOOL
        self.a.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,W.LPVOID,DWORD,C.POINTER(DWORD)];self.a.GetTokenInformation.restype=W.BOOL
        self.pressed=False;self.pressed_backend='sendinput';self.lock=threading.RLock();self.mutex=None
        assert C.sizeof(INPUT)==(40 if C.sizeof(C.c_void_p)==8 else 28)

    def single_instance(self):
        C.set_last_error(0)
        self.mutex=self.k.CreateMutexW(None,False,'Local\\TataFishingDesktopAssistant_v020')
        if not self.mutex:raise C.WinError(C.get_last_error())
        return C.get_last_error()!=183
    def key(self,vk):return bool(self.u.GetAsyncKeyState(vk)&0x8000)
    def cursor(self):
        p=W.POINT()
        if not self.u.GetCursorPos(C.byref(p)):raise C.WinError(C.get_last_error())
        return (p.x,p.y)
    def root_at(self,p):
        hwnd=self.u.WindowFromPoint(W.POINT(*map(round,p)))
        return self.u.GetAncestor(hwnd,2) or hwnd
    def title(self,hwnd):
        b=C.create_unicode_buffer(1024);self.u.GetWindowTextW(hwnd,b,len(b));return b.value
    def window_rect(self,hwnd):
        r=W.RECT()
        if not self.u.GetWindowRect(hwnd,C.byref(r)):raise C.WinError(C.get_last_error())
        return (r.left,r.top,r.right,r.bottom)
    def foreground_root(self):
        hwnd=self.u.GetForegroundWindow()
        return (self.u.GetAncestor(hwnd,2) or hwnd) if hwnd else None
    def activate(self,hwnd):
        if not hwnd or not self.u.IsWindow(hwnd) or self.u.IsIconic(hwnd):return False
        self.u.SetForegroundWindow(hwnd)
        return self.foreground_root()==hwnd
    def windows(self):
        out=[]
        callback=C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        @callback
        def visit(hwnd,_):
            if self.u.IsWindowVisible(hwnd):
                title=self.title(hwnd)
                if title:out.append((int(hwnd),title))
            return True
        self.u.EnumWindows.argtypes=[callback,W.LPARAM];self.u.EnumWindows.restype=W.BOOL
        self.u.EnumWindows(visit,0)
        return out
    def candidates(self):
        # Prefer the actual game, never this helper or a browser window.
        wins=self.windows();strict=[];other=[]
        for hwnd,title in wins:
            low=title.lower()
            if ('mumu' in low and ('powered' in low or '\u5854\u5854\u5192\u9669\u961f' in title)):
                strict.append((hwnd,title))
            elif 'mumu' in low and not any(s in low for s in ('chrome','edge','firefox','manager','\u591a\u5f00')):
                other.append((hwnd,title))
        return strict or other
    def old_helpers(self):
        return [(h,t) for h,t in self.windows() if t.startswith('\u5854\u5854\u9493\u9c7c\u52a9\u624b') and '0.1' in t]
    def elevation(self,hwnd=None):
        process=None;token=W.HANDLE()
        try:
            if hwnd:
                pid=DWORD();self.u.GetWindowThreadProcessId(hwnd,C.byref(pid))
                process=self.k.OpenProcess(0x1000,False,pid.value)
                if not process:return None
            else:process=self.k.GetCurrentProcess()
            if not self.a.OpenProcessToken(process,0x0008,C.byref(token)):return None
            value=DWORD();length=DWORD()
            if not self.a.GetTokenInformation(token,20,C.byref(value),C.sizeof(value),C.byref(length)):return None
            return bool(value.value)
        finally:
            if token.value:self.k.CloseHandle(token)
            if hwnd and process:self.k.CloseHandle(process)
    def guard_reason(self,hwnd,original_rect=None,point=None):
        if not hwnd or not self.u.IsWindow(hwnd):return 'window_missing'
        if self.u.IsIconic(hwnd):return 'window_minimized'
        if original_rect and any(abs(a-b)>2 for a,b in zip(self.window_rect(hwnd),original_rect)):
            return 'window_changed'
        if self.foreground_root()!=hwnd:return 'window_not_foreground'
        if point is not None and self.root_at(point)!=hwnd:return 'point_blocked'
        return None
    def safe(self,hwnd,rect=None,point=None):return self.guard_reason(hwnd,rect,point) is None
    def diagnostic(self,hwnd):
        return dict(target=hwnd,title=self.title(hwnd) if hwnd else None,
                    foreground=self.foreground_root(),
                    target_rect=self.window_rect(hwnd) if hwnd and self.u.IsWindow(hwnd) else None,
                    assistant_elevated=self.elevation(),target_elevated=self.elevation(hwnd) if hwnd else None,
                    cursor=self.cursor(),input_size=C.sizeof(INPUT))
    def send(self,flags,backend='sendinput'):
        if backend=='legacy':
            self.u.mouse_event(flags,0,0,0,0)
            return
        item=INPUT();item.type=0;item.mi=MOUSEINPUT(0,0,0,flags,0,0)
        sent=self.u.SendInput(1,C.byref(item),C.sizeof(INPUT))
        if sent!=1:raise RuntimeError('INPUT_REJECTED: SendInput count=%s winerror=%s'%(sent,C.get_last_error()))
    def move_to(self,point,cancel,guard,settle_ms=35):
        point=tuple(map(round,point))
        if cancel.is_set() or not guard(point):return False
        before=self.cursor()
        if abs(before[0]-point[0])+abs(before[1]-point[1])>1:
            if not self.u.SetCursorPos(*point):raise C.WinError(C.get_last_error())
            if cancel.wait(settle_ms/1000):return False
        actual=self.cursor()
        if max(abs(a-b) for a,b in zip(actual,point))>3:
            raise RuntimeError('CURSOR_MISMATCH: requested=%s actual=%s'%(point,actual))
        return not cancel.is_set() and guard(point)
    def release(self):
        with self.lock:
            if self.pressed:
                try:
                    try:self.send(0x0004,self.pressed_backend)
                    except Exception:self.send(0x0004,'legacy' if self.pressed_backend=='sendinput' else 'sendinput')
                finally:self.pressed=False
    def click(self,x,y,cancel,guard,hold_ms=90,settle_ms=35,backend='sendinput'):
        point=(round(x),round(y));started=time.perf_counter()
        if not self.move_to(point,cancel,guard,settle_ms):return None
        with self.lock:
            if cancel.is_set() or not guard(point):return None
            self.send(0x0002,backend);self.pressed=True;self.pressed_backend=backend
        down=time.perf_counter()
        try:
            cancel.wait(max(30,min(250,hold_ms))/1000)
        finally:self.release()
        up=time.perf_counter()
        return Receipt(point,self.cursor(),backend,down,up,(down-started)*1000)
