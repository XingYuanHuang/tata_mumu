"""MuMu background capture/touch. Never moves the desktop mouse or changes focus."""
import ctypes as C
import json
from pathlib import Path
import threading
import time
import numpy as np
import cv2
from windows_io import Receipt

def decode_pixels(raw,width,height):
    rgba=np.frombuffer(raw,np.uint8).reshape(height,width,4)
    return cv2.flip(cv2.cvtColor(rgba,cv2.COLOR_RGBA2BGRA),0)

def touch_coordinates(width,height,x,y):
    x,y=round(x),round(y)
    if not 0<=x<width or not 0<=y<height:raise ValueError('touch outside emulator display')
    return x,y

class Capture:
    def __init__(self,native):
        self.native=native
        self.monitors=[dict(left=0,top=0,width=native.width,height=native.height)]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def grab(self,region):
        im=self.native.screenshot()
        l,t,w,h=(region[k] for k in ('left','top','width','height'))
        if l<0 or t<0 or l+w>im.shape[1] or t+h>im.shape[0]:raise RuntimeError('capture region outside emulator')
        return im[t:t+h,l:l+w].copy()

class MuMuIO:
    background=True
    def __init__(self,config_file):
        c=json.loads(Path(config_file).read_text('utf-8-sig'))
        root=Path(c['root']);vm=c['vm'];index=int(c['instance'])
        if not root.is_absolute() or Path(vm).name!=vm or not 0<=index<=31:raise ValueError('invalid MuMu configuration')
        config=root/'vms'/vm/'configs'
        settings=json.loads((config/'vm_config.json').read_text('utf-8-sig'))
        port=settings['vm']['nat']['port_forward']['adb']['host_port']
        if c['serial']!='127.0.0.1:'+str(port):raise ValueError('MuMu instance does not match ADB device')
        keep=json.loads((config/'customer_config.json').read_text('utf-8-sig'))['customer']['app_keptlive']
        if str(keep).lower()!='false':raise ValueError('后台截图需要关闭模拟器多应用保活')
        dlls=[root/'nx_device/12.0/shell/sdk/external_renderer_ipc.dll',root/'shell/sdk/external_renderer_ipc.dll',root/'nx_main/sdk/external_renderer_ipc.dll']
        dll=next((p for p in dlls if p.is_file()),None)
        if dll is None:raise RuntimeError('未找到 MuMu 原生 SDK')
        self.lib=C.CDLL(str(dll))
        signatures={'nemu_connect':([C.c_wchar_p,C.c_int],C.c_int),'nemu_disconnect':([C.c_int],C.c_int),
          'nemu_capture_display':([C.c_int,C.c_int,C.c_int,C.POINTER(C.c_int),C.POINTER(C.c_int),C.c_void_p],C.c_int),
          'nemu_input_event_touch_down':([C.c_int,C.c_int,C.c_int,C.c_int],C.c_int),'nemu_input_event_touch_up':([C.c_int,C.c_int],C.c_int)}
        for name,(args,result) in signatures.items():f=getattr(self.lib,name);f.argtypes=args;f.restype=result
        self.connect_id=self.lib.nemu_connect(str(root),index)
        if self.connect_id<=0:raise RuntimeError('MuMu 后台连接失败，请启动模拟器')
        self.handle=index+1;self.serial=c['serial'];self.index=index
        self.lock=threading.Lock();self.pressed=False;self.closed=False
        self.width,self.height=self.resolution()
        from windows_io import WindowsIO
        self.desktop_keys=WindowsIO() # Only hotkey/mutex reads; no desktop input is used.
    def resolution(self):
        w,h=C.c_int(),C.c_int()
        ret=self.lib.nemu_capture_display(self.connect_id,0,0,C.byref(w),C.byref(h),None)
        if ret!=0 or not 1<=w.value<=4096 or not 1<=h.value<=4096:raise RuntimeError('MuMu 后台截图分辨率读取失败')
        return w.value,h.value
    def screenshot(self):
        with self.lock:
            width,height=self.resolution()
            if (width,height)!=(self.width,self.height):raise RuntimeError('模拟器分辨率已改变，请重新开始')
            buf=(C.c_ubyte*(width*height*4))();w,h=C.c_int(width),C.c_int(height)
            ret=self.lib.nemu_capture_display(self.connect_id,0,len(buf),C.byref(w),C.byref(h),buf)
            if ret!=0 or (w.value,h.value)!=(width,height):raise RuntimeError('MuMu 后台截图失败')
            return decode_pixels(buf,width,height)
    def capture_session(self):return Capture(self)
    def candidates(self):return [(self.handle,f'MuMu 后台实例 {self.index} · {self.serial}')]
    def old_helpers(self):return self.desktop_keys.old_helpers()
    def single_instance(self):return self.desktop_keys.single_instance()
    def key(self,vk):return False if vk==1 else self.desktop_keys.key(vk)
    def elevation(self,hwnd=None):return False
    def activate(self,hwnd):return hwnd==self.handle
    def foreground_root(self):return self.handle
    def window_rect(self,hwnd):return (0,0,self.width,self.height)
    def root_at(self,p):return self.handle if 0<=p[0]<self.width and 0<=p[1]<self.height else 0
    def guard_reason(self,hwnd,original_rect=None,point=None):
        if self.closed or hwnd!=self.handle:return 'window_missing'
        if original_rect is not None and tuple(original_rect)!=self.window_rect(hwnd):return 'window_changed'
        if point is not None and self.root_at(point)!=hwnd:return 'point_blocked'
        return None
    def safe(self,hwnd,rect=None,point=None):return self.guard_reason(hwnd,rect,point) is None
    def diagnostic(self,hwnd):return dict(backend='mumu-ipc',serial=self.serial,instance=self.index,resolution=[self.width,self.height],desktop_input=False,foreground_required=False)
    def release(self):
        with self.lock:
            if self.pressed:
                ret=self.lib.nemu_input_event_touch_up(self.connect_id,0)
                self.pressed=False
                if ret!=0:raise RuntimeError('MuMu touch release failed')
    def click(self,x,y,cancel,guard,hold_ms=90,settle_ms=35,backend='mumu-ipc'):
        p=(round(x),round(y));started=time.perf_counter()
        if cancel.is_set() or not guard(p):return None
        tx,ty=touch_coordinates(self.width,self.height,*p)
        with self.lock:
            if cancel.is_set() or not guard(p):return None
            if self.lib.nemu_input_event_touch_down(self.connect_id,0,tx,ty)!=0:raise RuntimeError('MuMu touch down failed')
            self.pressed=True
        down=time.perf_counter()
        try:cancel.wait(max(30,min(250,hold_ms))/1000)
        finally:self.release()
        up=time.perf_counter()
        return Receipt(p,p,'mumu-ipc',down,up,(down-started)*1000)
    def close(self):
        if self.closed:return
        self.release()
        self.lib.nemu_disconnect(self.connect_id);self.closed=True
