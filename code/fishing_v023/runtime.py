"""Thread-owned capture/input runner. UI interactions travel only through a queue."""
from __future__ import annotations
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import csv
from pathlib import Path
import platform
import threading
import time
import traceback
import zipfile
import cv2
import numpy as np
from controller import Controller, Motion
from locator import Locator, Region
from vision import Vision, annotate, WIDTH, HEIGHT
from zh import tr

@dataclass
class Settings:
    mode: str='single'
    limit: int=3
    latency: float=35
    jitter: bool=False
    backend: str='sendinput'
    edge: str='both'

class StopRun(Exception):pass

def clip_window(rect, desktop):
    l,t,r,b=rect;dl,dt=desktop['left'],desktop['top']
    dr,db=dl+desktop['width'],dt+desktop['height']
    l,t,r,b=max(l,dl),max(t,dt),min(r,dr),min(b,db)
    if r-l<230 or b-t<420:raise StopRun('outside_screen')
    return Region(l,t,r-l,b-t)

def counter_crop(frame):
    # Digits on the dark resource badge, NOT animated water, title or button.
    return cv2.cvtColor(frame[918:953,264:346],cv2.COLOR_BGR2GRAY)

def counters_similar(a,b):
    if a is None or b is None or a.shape!=b.shape:return False
    return float(np.mean(cv2.absdiff(a,b)))<4.0

def collect_is_safe(action, observation):
    """Recheck after a proposal so a dismissed card cannot leak a click to QTE."""
    if not observation.can_collect:
        return False
    if action.kind == 'COLLECT':
        return observation.state == 'REWARD'
    if action.kind in ('RESULT','RECORD','CONTINUE'):
        return observation.state in ('RESULT','RECORD','CONTINUE')
    return False

def input_point(native, region, action, observation):
    # The close/continue area sits below the cropped fishing canvas.
    bottom_recovery = action.kind == 'RECOVER' and observation.state == 'UNKNOWN'
    card_retry = (action.retry and action.kind in ('RESULT', 'RECORD', 'CONTINUE')
                  and observation.state in ('RESULT', 'RECORD', 'CONTINUE'))
    if getattr(native, 'background', False) and (bottom_recovery or card_retry):
        return round(native.width * .5), round(native.height * .96)
    return region.point(*action.point)


def fishing_speed(casts, elapsed):
    elapsed = max(0., elapsed)
    return dict(elapsed_seconds=round(elapsed, 2),
                casts_per_minute=round(casts * 60 / elapsed, 2) if elapsed > 0 else 0.)


class Session:
    def __init__(self,data_dir,post):
        self.path=Path(data_dir)/'logs'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.path.mkdir(parents=True,exist_ok=True)
        self.post=post;self.lock=threading.Lock();self.frames=deque(maxlen=14);self.keyframes=deque(maxlen=12)
        self.latest=None;self.obs=None;self.sequence=0;self.metadata={}
    def log(self,text):
        line='[%s] %s\n'%(datetime.now().strftime('%H:%M:%S.%f')[:-3],text)
        with self.lock:
            with (self.path/'session.log').open('a',encoding='utf-8') as f:f.write(line)
        self.post('log',line)
    def record_speed(self, stats):
        fields = ('timestamp', 'elapsed_seconds', 'casts', 'collected', 'casts_per_minute')
        path = self.path/'speed.csv'
        new = not path.exists()
        with path.open('a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if new:writer.writeheader()
            writer.writerow(dict(timestamp=datetime.now().isoformat(timespec='seconds'),
                                 **{key:stats[key] for key in fields[1:]}))
        self.metadata['speed'] = {key:stats[key] for key in fields[1:]}
        self.log('钓鱼效率：%.2f 竿/分 · 运行 %.1f 秒 · 已确认 %d 竿' %
                 (stats['casts_per_minute'], stats['elapsed_seconds'], stats['casts']))
    def remember(self,frame,obs):
        self.latest=frame;self.obs=obs
        self.frames.append((time.time(),frame.copy(),obs))
    def keyframe(self,name,frame,obs):
        self.keyframes.append((name,frame.copy(),obs))
    def save(self,name,frame=None,obs=None):
        frame=self.latest if frame is None else frame;obs=self.obs if obs is None else obs
        if frame is None:return
        self.sequence+=1
        rendered=annotate(frame,obs) if obs else frame
        stem='%03d_%s'%(self.sequence,name)
        cv2.imencode('.png',rendered)[1].tofile(str(self.path/(stem+'.png')))
        # Keep original game pixels too; preview overlays must not hide evidence.
        if name=='final' or name.startswith('before_HOOK') or name.startswith('before_RESULT') or name.startswith('before_CONTINUE') or name.startswith('before_RECORD'):
            cv2.imencode('.png',frame)[1].tofile(str(self.path/(stem+'_raw.png')))
    def finalize(self,reason):
        self.metadata['stop_reason']=reason
        (self.path/'session.json').write_text(json.dumps(self.metadata,ensure_ascii=False,indent=2,default=str),'utf-8')
        for name,frame,obs in self.keyframes:self.save(name,frame,obs)
        for i,(_,frame,obs) in enumerate(list(self.frames)[-10:]):self.save('recent_%02d'%i,frame,obs)
        self.save('final')
    def export(self,destination):
        dest=Path(destination)
        with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(self.path.iterdir()):
                if p.is_file():z.write(p,p.name)
        return dest

class Runner:
    def __init__(self,native,assets,session,settings,hwnd,cancel,post):
        self.native=native;self.assets=Path(assets);self.session=session;self.settings=settings
        self.hwnd=hwnd;self.cancel=cancel;self.post=post
        self.region=None;self.wrect=None;self.ctl=None
    def status(self,key):self.post('status',tr(key))
    def locate(self,screen,locator):
        before=self.native.window_rect(self.hwnd)
        box=clip_window(before,screen.monitors[0])
        raw=np.asarray(screen.grab(box.grab()))[:,:,:3].copy()
        found=locator.locate(raw)
        if self.native.window_rect(self.hwnd)!=before:return None
        if not found:return None
        self.wrect=before;self.region=found.region.translated(box.left,box.top)
        self.session.log('AUTO_ROI %s score=%.3f state=%s'%(self.region.grab(),found.score,found.state))
        self.session.metadata['region']=self.region.grab()
        self.session.metadata['window_rect']=before
        return found
    def check_visibility(self):
        r=self.region
        points=[r.point(295,858),r.point(100,85),r.point(485,85),r.point(100,850),r.point(485,850)]
        for p in points:
            if self.native.root_at(p)!=self.hwnd:raise StopRun('point_blocked')
    def run(self):
        reason='stopped';old_state='';counter_before=None;after_input=None
        speed_started=None
        counts=dict(casts=0,qte=0,attempts=0,collected=0)
        try:
            mss=None
            if not hasattr(self.native,'capture_session'):
                import mss
            cv2.setNumThreads(1)
            self.session.metadata.update(version='0.2.3',settings=asdict(self.settings),
                python=platform.python_version(),platform=platform.platform(),opencv=cv2.__version__,
                numpy=np.__version__,mss=mss.__version__ if mss else 'unused: MuMu IPC capture')
            self.session.metadata['native']=self.native.diagnostic(self.hwnd)
            self.session.log('ENV '+json.dumps(self.session.metadata,ensure_ascii=False,default=str))
            if self.native.elevation(self.hwnd) is True and self.native.elevation() is False:raise StopRun('privilege')
            self.native.activate(self.hwnd)
            focus_started=time.perf_counter()
            while self.native.foreground_root()!=self.hwnd:
                if self.cancel.wait(.08):return
                self.status('refocus')
                if time.perf_counter()-focus_started>10:raise StopRun('window_not_foreground')
            with (self.native.capture_session() if hasattr(self.native,'capture_session') else mss.mss()) as screen:
                locator=Locator(self.assets);self.status('locating');found=None
                for _ in range(4):
                    if self.cancel.is_set():return
                    problem=self.native.guard_reason(self.hwnd)
                    if problem:raise StopRun(problem)
                    found=self.locate(screen,locator)
                    if found:break
                    if self.cancel.wait(.30):return
                if not found:raise StopRun('no_canvas')
                self.check_visibility();self.status('located')
                limit=1 if self.settings.mode=='single' else self.settings.limit
                if found.state!='READY' and self.settings.mode=='single':limit=0
                self.ctl=Controller(limit,self.settings.jitter,self.settings.latency,
                                    backend=self.settings.backend,edge=self.settings.edge)
                vision=Vision(self.assets);watch=Motion()
                n=0;fps=0.;rate_started=time.perf_counter();last_preview=0.;last_memory=0.
                speed_started=rate_started;last_speed=rate_started
                slow_since=None;last_visibility=0.;last_backend=None
                while not self.cancel.is_set():
                    loop=time.perf_counter()
                    problem=self.native.guard_reason(self.hwnd)
                    if problem:raise StopRun(problem)
                    current=self.native.window_rect(self.hwnd)
                    if any(abs(a-b)>2 for a,b in zip(current,self.wrect)):
                        self.native.release();self.status('relocating');self.session.log(tr('relocating'))
                        stable=False
                        for _ in range(18):
                            before=self.native.window_rect(self.hwnd)
                            if self.cancel.wait(.18):return
                            if before==self.native.window_rect(self.hwnd):stable=True;break
                        if not stable:raise StopRun('window_changed')
                        if not self.locate(screen,locator):raise StopRun('no_canvas')
                        self.ctl.motion.reset();watch.reset();vision.reset();self.check_visibility()
                        old_state='';continue
                    if loop-last_visibility>.35:
                        self.check_visibility();last_visibility=loop
                    t0=time.perf_counter();raw=np.asarray(screen.grab(self.region.grab()))[:,:,:3]
                    t1=time.perf_counter();captured=(t0+t1)/2
                    frame=cv2.resize(raw,(WIDTH,HEIGHT));obs=vision.analyze(frame,captured)
                    age=time.perf_counter()-captured
                    self.session.latest=frame;self.session.obs=obs
                    if after_input:
                        self.session.keyframe('after_'+after_input,frame,obs);after_input=None
                    if captured-last_memory>.24:
                        self.session.remember(frame,obs);last_memory=captured
                    if obs.state!=old_state:
                        self.session.log('STATE '+tr(obs.state)+' '+str(obs.scores))
                        self.status(obs.state);old_state=obs.state
                    if self.settings.mode!='observe':
                        if obs.state=='QTE' and age>.14:
                            if slow_since is None:slow_since=captured
                            if captured-slow_since>1.2:raise StopRun('slow_capture')
                            self.ctl.motion.reset();act=None
                        else:
                            slow_since=None
                            act=self.ctl.step(obs,captured,age,counters_similar(counter_before,counter_crop(frame)))
                        for code,info in self.ctl.drain_events():
                            self.session.log(tr(code)+' '+tr(info.get('kind',''))+' '+str(info))
                            if code=='confirmed' and last_backend:
                                self.ctl.backend=last_backend
                        if act:
                            if act.kind=='STOP':raise StopRun(act.reason)
                            if act.kind=='CAST':
                                # v0.2.3: multiplier is a user choice. x1/x2/x5/x10/etc.
                                # are all allowed; only remember the counter for diagnostics.
                                if not act.retry:counter_before=counter_crop(frame).copy()
                            if act.kind=='HOOK':
                                # A proposed hook can go stale between classification and input.
                                # Capture again, rather than clicking a newly started QTE.
                                fresh_start=time.perf_counter()
                                fresh=np.asarray(screen.grab(self.region.grab()))[:,:,:3]
                                fresh_frame=cv2.resize(fresh,(WIDTH,HEIGHT))
                                fresh_obs=vision.analyze(fresh_frame,time.perf_counter())
                                if fresh_obs.state!='BITE' or time.perf_counter()-fresh_start>.14:
                                    self.ctl.withdraw_hook()
                                    self.session.log(tr('hook_skipped')+' fresh_state='+fresh_obs.state)
                                    self.session.keyframe('hook_skipped',fresh_frame,fresh_obs)
                                    continue
                                frame,obs=fresh_frame,fresh_obs
                                self.ctl.hook_fraction=obs.bite_fraction
                            if act.kind in ('COLLECT','RESULT','RECORD','CONTINUE'):
                                fresh_start=time.perf_counter()
                                fresh=np.asarray(screen.grab(self.region.grab()))[:,:,:3]
                                fresh_frame=cv2.resize(fresh,(WIDTH,HEIGHT))
                                fresh_obs=vision.analyze(fresh_frame,time.perf_counter())
                                if not collect_is_safe(act,fresh_obs) or time.perf_counter()-fresh_start>.20:
                                    self.ctl.withdraw_collect()
                                    self.session.log(tr('collect_skipped')+' fresh_state='+fresh_obs.state)
                                    continue
                                frame,obs=fresh_frame,fresh_obs
                                if obs.control is not None:act.point=obs.control
                            if act.kind=='RECOVER':
                                fresh=np.asarray(screen.grab(self.region.grab()))[:,:,:3]
                                fresh_obs=vision.analyze(cv2.resize(fresh,(WIDTH,HEIGHT)),time.perf_counter())
                                if fresh_obs.state!='UNKNOWN':continue
                            if self.native.key(1) and not self.native.pressed:raise StopRun('manual_mouse')
                            x,y=input_point(self.native,self.region,act,obs)
                            guard=lambda p:self.native.safe(self.hwnd,self.wrect,p)
                            # Bounded in-memory snapshots; no PNG encoding in the active control loop.
                            if act.kind!='QTE':self.session.keyframe('before_'+act.kind,frame,obs)
                            receipt=self.native.click(x,y,self.cancel,guard,hold_ms=act.hold_ms,
                                                      settle_ms=0 if act.kind in ('QTE','HOOK') else 40,
                                                      backend=act.backend)
                            if receipt is None:
                                if self.cancel.is_set():return
                                raise StopRun(self.native.guard_reason(self.hwnd,self.wrect,(x,y)) or 'input_cancelled')
                            self.ctl.input_sent(act,receipt)
                            if act.kind=='QTE':self.ctl.qte_count+=1
                            last_backend=receipt.backend
                            self.session.log('INPUT %s%s requested=%s actual=%s backend=%s hold=%.1fms move=%.1fms %s'%(
                                tr(act.kind),' [retry]' if act.retry else '',receipt.requested,receipt.actual,
                                receipt.backend,receipt.hold_ms,receipt.move_ms,act.reason))
                            # Keep the pre-click QTE frame after sending, without blocking on disk IO.
                            if act.kind=='QTE':self.session.keyframe('before_QTE',frame,obs)
                            after_input=act.kind
                    else:
                        if obs.state=='QTE' and obs.x is not None and not obs.flash:watch.add(captured,obs.x)
                        else:watch.reset()
                    n+=1;now=time.perf_counter()
                    counts=dict(casts=self.ctl.cast_count,qte=self.ctl.qte_count,
                                attempts=self.ctl.cast_attempts,collected=self.ctl.collect_count)
                    speed=fishing_speed(counts['casts'],now-speed_started)
                    if now-last_speed>=10:
                        self.session.record_speed(dict(**counts,**speed));last_speed=now
                    if now-rate_started>=.8:
                        fps=n/(now-rate_started);n=0;rate_started=now
                        self.post('stats',dict(fps=fps,**counts,**speed))
                        self.post('hook_stats',dict(hooks=self.ctl.hook_input_count,unverified=self.ctl.hook_unverified))
                    if now-last_preview>.4:
                        preview=annotate(frame,obs,self.ctl.predicted)
                        cv2.drawMarker(preview,(295,858),(30,220,30),cv2.MARKER_CROSS,20,2)
                        preview=cv2.resize(preview,(218,359))
                        ppm=cv2.imencode('.ppm',preview)[1].tobytes()
                        self.post('preview',ppm);last_preview=now
                    self.cancel.wait(max(.001,.011-(time.perf_counter()-loop)))
        except StopRun as exc:
            reason=str(exc);self.session.log('END '+reason+' '+tr(reason))
        except Exception as exc:
            reason='error';self.session.log(traceback.format_exc())
            self.session.metadata['exception']=repr(exc)
            self.post('detail',tr('error')+str(exc))
        finally:
            self.cancel.set()
            try:self.native.release()
            except Exception:self.session.log('RELEASE ERROR '+traceback.format_exc())
            if self.ctl:
                counts=dict(casts=self.ctl.cast_count,qte=self.ctl.qte_count,attempts=self.ctl.cast_attempts,collected=self.ctl.collect_count)
            if self.ctl:
                hookinfo=dict(inputs=self.ctl.hook_input_count,
                              prompt_followups=self.ctl.hook_followups,
                              unverified=self.ctl.hook_unverified,
                              note='OS inputs and visual timing only; not proof that input caused the transition.')
                self.session.metadata['hook_diagnostics']=hookinfo
                self.post('hook_stats',dict(hooks=hookinfo['inputs'],unverified=hookinfo['unverified']))
            self.session.metadata['counts']=counts
            speed=fishing_speed(counts['casts'],time.perf_counter()-speed_started if speed_started is not None else 0)
            try:self.session.record_speed(dict(**counts,**speed))
            except Exception:self.session.log('SPEED SAVE ERROR '+traceback.format_exc())
            try:self.session.finalize(reason)
            except Exception:self.session.log('SAVE ERROR '+traceback.format_exc())
            self.post('stats',dict(fps=0,**counts,**speed))
            self.post('finished',reason)
