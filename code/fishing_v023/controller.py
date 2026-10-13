"""Visual acknowledgements, bounded retries and per-round QTE locks.

A proposed or OS-accepted click never increments the confirmed cast count.
Synthetic/offline tests can inspect every transition without sending input.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import random
import numpy as np
from vision import Observation

@dataclass
class Action:
    kind: str
    point: tuple[float,float] | None = None
    reason: str = ''
    retry: bool = False
    hold_ms: int = 90
    backend: str = 'sendinput'

class Motion:
    def __init__(self):self.samples=deque(maxlen=24)
    def reset(self):self.samples.clear()
    def add(self,t,x):
        if self.samples:
            dt=t-self.samples[-1][0]
            if dt<=0:return
            if dt>.18 or abs(x-self.samples[-1][1])>max(25,dt*1100):self.reset()
        self.samples.append((t,x))
        while self.samples and t-self.samples[0][0]>.105:self.samples.popleft()
    def velocity(self):
        if len(self.samples)<3 or self.samples[-1][0]-self.samples[0][0]<.030:return None
        a=np.asarray(self.samples,dtype=float);d=np.diff(a[:,1]);nz=d[np.abs(d)>.6]
        if len(nz)<2 or (np.any(nz>0) and np.any(nz<0)):return None
        ts=a[:,0]-a[-1,0];centered=ts-ts.mean();den=float(centered@centered)
        if den<1e-8:return None
        v=float(centered@(a[:,1]-a[:,1].mean())/den)
        residual=float(np.sqrt(np.mean((a[:,1]-(a[:,1].mean()+v*centered))**2)))
        return v if 15<abs(v)<1200 and residual<=3.5 else None

class Controller:
    def __init__(self,max_casts=3,jitter=False,latency_ms=35,seed=None,backend='sendinput',edge='both'):
        self.max_casts=int(max_casts);self.jitter=jitter;self.latency=float(latency_ms)/1000
        self.rng=random.Random(seed);self.backend=backend;self.edge=edge
        self.cast_count=0;self.cast_attempts=0;self.qte_count=0;self.collect_count=0
        self.state='';self.state_since=0.;self.started=None;self.last_progress=0.
        self.pending=None;self.pending_time=0.;self.pending_retries=0
        self.confirm_since=None;self.events=[];self.delay=.4
        self.motion=Motion();self.predicted=None
        self.qte_active=False;self.qte_since=None;self.qte_locked=False;self.qte_ack=False
        self.qte_time=-999.;self.clicked_bounds=None;self.left_target=False
        self.flash_last=False;self.flash_end=-999.;self.changed_since=None
        self.qte_hold_ms=45
        self.hook_input_count=0;self.hook_followups=0;self.hook_unverified=0
        self.hook_window=False;self.hook_followed=False;self.hook_cycle_inputs=0
        self.hook_first_seen=None;self.hook_last_release=None;self.hook_fraction=None
        self.hook_live_since=None
        self.unknown_since=None;self.recovery_count=0;self.recovery_time=-999.
        self.recovery_sent=False

    def event(self,code,**data):self.events.append((code,data))
    def drain_events(self):out=self.events;self.events=[];return out
    def stop(self,reason):return Action('STOP',reason=reason)
    def point(self,x,y):
        if self.jitter:return (x+self.rng.uniform(-2,2),y+self.rng.uniform(-2,2))
        return (x,y)
    def act(self,kind,t,retry=False,reason='',point=None):
        if not retry:
            self.pending=kind;self.pending_retries=0
        else:self.pending_retries+=1
        self.pending_time=t;self.confirm_since=None
        if kind=='CAST':
            self.cast_attempts+=1
            if not retry:
                self.hook_window=True;self.hook_followed=False;self.hook_cycle_inputs=0
                self.hook_first_seen=None;self.hook_last_release=None;self.hook_fraction=None
                self.hook_live_since=None
        p=point or ((295,920) if kind=='COLLECT' else ((295,570) if kind in ('RECORD','RESULT','CONTINUE') else (295,858)))
        # Do not randomize hook timing or its landing point.
        point=p if kind=='HOOK' else self.point(*p)
        return Action(kind,point,reason,retry,150 if retry else (110 if kind=='HOOK' else 90),
                      'legacy' if retry and self.backend=='sendinput' else self.backend)
    def input_sent(self, action, receipt):
        """Only an actual OS receipt counts as an input, never a proposal/timeout."""
        if action.kind=='RECOVER':self.recovery_sent=True
        if self.pending==action.kind:self.pending_time=receipt.up_at
        if action.kind=='HOOK':
            self.hook_input_count+=1;self.hook_cycle_inputs+=1
            self.hook_last_release=receipt.up_at
            self.event('hook_sent',kind='HOOK',retry=action.retry,
                       seen_to_down_ms=round(1000*(receipt.down_at-self.hook_first_seen),1)
                         if self.hook_first_seen is not None else None,
                       remaining_fraction=self.hook_fraction)

    def observe_hook_progress(self, o, t):
        if o.state in ('WAIT','BITE') and not self.hook_followed:
            self.hook_window=True
        if o.state=='BITE':
            if self.hook_first_seen is None:self.hook_first_seen=t
            if self.hook_live_since is None:self.hook_live_since=t
            self.hook_fraction=o.bite_fraction
        else:self.hook_live_since=None
        if (o.state in ('QTE','REWARD','RECORD','RESULT','CONTINUE') and self.hook_window
                and not self.hook_followed):
            self.hook_followed=True
            lag=None if self.hook_last_release is None else max(0.,t-self.hook_last_release)
            if self.hook_cycle_inputs and lag is not None and lag<=.65:
                self.hook_followups+=1
                self.event('hook_followup',kind='HOOK',after_release_ms=round(lag*1000,1),
                           causality_verified=False)
            else:
                self.hook_unverified+=1
                self.event('hook_unverified',kind='HOOK',inputs=self.hook_cycle_inputs,
                           after_release_ms=None if lag is None else round(lag*1000,1))

    def withdraw_hook(self):
        # A new capture showed that the prompt had gone. Never send this old
        # proposed hook into a newly visible QTE/result panel.
        if self.pending=='HOOK':self.pending=None;self.confirm_since=None
        self.event('hook_skipped',kind='HOOK')

    def withdraw_collect(self):
        if self.pending in ('COLLECT','RESULT','RECORD','CONTINUE'):
            self.pending=None;self.confirm_since=None
        self.event('collect_skipped')

    def reset_qte(self):
        self.qte_active=False;self.qte_since=None;self.qte_locked=False;self.qte_ack=False
        self.motion.reset();self.changed_since=None;self.flash_last=False
    def _pending_step(self,o,t,same_counter):
        kind=self.pending
        good={
            'CAST':('WAIT','BITE','QTE','REWARD','RECORD','RESULT','CONTINUE'),
            'HOOK':('QTE','REWARD','RECORD','RESULT','CONTINUE'),
            'COLLECT':('READY','RECORD','RESULT','CONTINUE'),
            'RECORD':('REWARD','READY'),
            'RESULT':('REWARD','READY'),
            'CONTINUE':('REWARD','READY'),
        }[kind]
        if o.state in good:
            if self.confirm_since is None:self.confirm_since=t
            # Fast hook/QTE recognition must not be held up by a long debounce.
            duration=.035 if kind!='CAST' else .065
            if kind=='CAST' and o.state=='BITE' and o.scores.get('bite_source'):
                duration=0.  # A live, independently verified prompt is urgent.
            if t-self.confirm_since>=duration:
                if kind=='CAST':self.cast_count+=1
                if kind=='COLLECT' or (kind in ('RESULT','RECORD','CONTINUE') and o.state=='READY'):
                    self.collect_count+=1
                if kind!='HOOK':self.event('confirmed',kind=kind,casts=self.cast_count)
                self.pending=None;self.confirm_since=None;self.last_progress=t
            return None
        self.confirm_since=None
        elapsed=t-self.pending_time
        unchanged={'CAST':'READY','HOOK':'BITE','COLLECT':'REWARD','RECORD':'RECORD','RESULT':'RESULT','CONTINUE':'CONTINUE'}[kind]
        card_action=kind in ('RESULT','RECORD','CONTINUE')
        threshold=2.8 if kind=='CAST' else (.28 if kind=='HOOK' else (1.0 if card_action else 2.2))
        same_page=(o.state in ('RESULT','RECORD','CONTINUE')) if card_action else o.state==unchanged
        retry_limit=2 if card_action else 1
        if (same_page and elapsed>=threshold and self.pending_retries<retry_limit
                and (kind not in ('COLLECT','RECORD','RESULT','CONTINUE') or o.can_collect)
                and (kind!='HOOK' or (self.hook_live_since is not None and t-self.hook_live_since>=.025))):
            # v0.2.3 tool mode: do not block a retry merely because the
            # consumable counter changed (higher multipliers legitimately
            # change it by more than one). The existing retry budget still
            # prevents a tight click loop.
            self.event('retry',kind=kind)
            return self.act(kind,t,retry=True,reason='bounded_retry',point=o.control)
        if card_action and self.pending_retries>=retry_limit and elapsed>1.6:
            return self.stop('result_stuck')
        if elapsed>(3.5 if self.pending_retries else 6.5):
            return self.stop('input_no_effect' if kind=='CAST' else 'action_no_effect')
        return None
    def step(self,o:Observation,t:float,processing_delay=0.,same_counter=True):
        if self.started is None:self.started=t;self.last_progress=t
        self.observe_hook_progress(o,t)
        if o.state!=self.state:
            self.state=o.state;self.state_since=t
            if o.state=='READY':self.delay=self.rng.uniform(.35,.6) if self.jitter else .40
            elif o.state=='BITE':
                self.delay=0. if o.scores.get('bite_source') else .025
            elif o.state in ('REWARD','RECORD','RESULT'):self.delay=.60
            elif o.state=='CONTINUE':self.delay=1.0
        if o.state=='UNKNOWN':
            if self.unknown_since is None:self.unknown_since=t
            if t-self.unknown_since>=1.2 and t-self.recovery_time>=1.2:
                if self.recovery_count>=3:return self.stop('unknown_screen_stuck')
                self.recovery_count+=1;self.recovery_time=t
                if self.pending:self.pending_time=t
                return Action('RECOVER',(80,780),'unknown_blank_retry',self.recovery_count>1,90,self.backend)
            return None
        self.unknown_since=None;self.recovery_count=0
        if self.recovery_sent and o.state=='READY':
            if t-self.state_since<.065:return None
            # A bottom dismissal returned to the fishing page. An unrecognized
            # result may have hidden the hook acknowledgment; do not await it forever.
            if self.pending in ('CAST','HOOK'):
                self.pending=None;self.confirm_since=None;self.reset_qte()
                self.event('recovered_ready')
            self.recovery_sent=False
        elif o.state in ('WAIT','BITE','QTE','REWARD','RESULT','RECORD','CONTINUE'):
            self.recovery_sent=False
        if self.pending:
            result=self._pending_step(o,t,same_counter)
            if result or self.pending:return result
        if o.state in ('READY','WAIT','BITE','REWARD','RECORD','RESULT','CONTINUE') and self.qte_active:
            self.reset_qte()
        if o.state in ('WAIT','TRANSITION','UNKNOWN'):
            # Tool mode: keep observing instead of terminating just because a
            # transition, wait, or temporarily unknown screen lasts longer.
            return None
        if o.state=='READY':
            if self.cast_count>=self.max_casts:return self.stop('cast_limit')
            if t-self.state_since<self.delay:return None
            self.last_progress=t
            return self.act('CAST',t)
        if o.state=='BITE':
            if t-self.state_since<self.delay:return None
            self.last_progress=t
            return self.act('HOOK',t)
        if o.state in ('REWARD','RECORD','RESULT','CONTINUE'):
            if not o.can_collect or t-self.state_since<self.delay:return None
            self.last_progress=t
            return self.act('COLLECT' if o.state=='REWARD' else o.state,t,point=o.control)
        if o.state!='QTE':return None
        if not self.qte_active:
            self.qte_active=True;self.qte_since=t;self.motion.reset()
        if t-self.qte_since>38:return self.stop('qte_timeout')
        if o.flash:
            if self.qte_locked and t-self.qte_time>.025:self.qte_ack=True
            self.flash_last=True;self.motion.reset();self.predicted=None
            return None
        if self.flash_last:self.flash_last=False;self.flash_end=t
        if o.x is not None:self.motion.add(t,o.x)
        else:self.motion.reset()
        if self.qte_locked:
            changed=False
            if self.clicked_bounds and o.target:
                l0,r0=self.clicked_bounds;l,r=o.target
                changed=max(abs(l-l0),abs(r-r0))>7 or abs((r-l)-(r0-l0))>6
            if changed:
                if self.changed_since is None:self.changed_since=t
            else:self.changed_since=None
            stable_new=self.changed_since is not None and t-self.changed_since>=.075
            if o.x is not None and self.clicked_bounds:
                l,r=self.clicked_bounds
                if o.x<l-4 or o.x>r+4:self.left_target=True
            # New smaller target can acknowledge a round even if a flash frame was missed.
            clear_edges=(o.x is not None and o.target is not None and
                         abs(o.x-o.target[0])>23 and abs(o.x-o.target[1])>23)
            if stable_new and clear_edges and t-self.qte_time>.22:self.qte_ack=True
            if self.qte_ack and (stable_new or self.left_target) and t-self.qte_time>.30 and t-self.flash_end>.08:
                self.qte_locked=False;self.qte_ack=False;self.left_target=False
                self.changed_since=None;self.motion.reset();self.last_progress=t
            else:
                if not self.qte_ack and t-self.qte_time>3.2:return self.stop('qte_no_feedback')
                return None
        if o.x is None or o.target is None:return None
        v=self.motion.velocity()
        if v is None:return None
        l,r=o.target;width=r-l
        if width<9:return None
        delay=self.latency+max(0,min(.12,processing_delay))
        down=o.x+v*delay;up=down+v*self.qte_hold_ms/1000
        margin=max(2.5,min(10,width*.14))
        self.predicted=up
        inside=lambda x:l+margin<=x<=r-margin
        if self.edge=='down':eligible=inside(down)
        elif self.edge=='up':eligible=inside(up)
        else:
            # Keep both edges in yellow when possible. For very narrow targets,
            # use release timing explicitly rather than adding a random QTE wait.
            both_possible=width-2*margin>abs(v)*self.qte_hold_ms/1000+3
            eligible=inside(down) and inside(up) if both_possible else inside(up)
        if eligible:
            self.qte_locked=True;self.qte_ack=False;self.left_target=False
            self.qte_time=t;self.clicked_bounds=o.target;self.changed_since=None;self.last_progress=t
            return Action('QTE',(295,858),
                          'x=%.1f speed=%.1f down=%.1f up=%.1f target=%.1f..%.1f'%(o.x,v,down,up,l,r),
                          False,self.qte_hold_ms,self.backend)
        return None
