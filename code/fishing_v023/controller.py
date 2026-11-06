"""Visual acknowledgements, bounded retries and per-round QTE locks.

A proposed or OS-accepted click never increments the confirmed cast count.
Synthetic/offline tests can inspect every transition without sending input.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import os
import random
import numpy as np
from vision import Observation

# 每 N 次 QTE 点击安排一次"人为失误"（N 在区间内随机）。
# 可用环境变量 FISH_MISS_MIN / FISH_MISS_MAX 覆盖，便于调试。
MISS_RANGE=(max(2,int(os.environ.get('FISH_MISS_MIN','20'))),
            max(2,int(os.environ.get('FISH_MISS_MAX','30'))))

# 撒鱼饵：右侧奖励上方的鱼饵包按钮（画布坐标 588x968）。
BAIT_POINT=(527,788)
# 等级徽章上限（图鉴上方的数字），达到后停止撒饵。
BAIT_LEVEL_MAX=8

# QTE 僵死看门狗（与快/慢轮型无关的通用兜底）：
#   一级 FISH_QTE_FALLBACK_IN（默认 5s）：QTE 持续无任何点击达此时长，
#     且游标在黄区内 → 立即点击；
#   二级（一级 +2.5s）：无视游标/黄区识别结果，盲点一次强行推进。
#   盲点失败时按"失误宽限"处理（0.9s 后解锁继续），不会终止会话。
QTE_FALLBACK_IN=max(2.,float(os.environ.get('FISH_QTE_FALLBACK_IN','5.0')))
QTE_FALLBACK_BLIND=QTE_FALLBACK_IN+2.5

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
        a=np.asarray(self.samples,dtype=float);d=np.diff(a[:,1])
        # 两遍估计：严格门限覆盖快速轮；宽松门限覆盖慢速漂移轮（帧位移<0.6px）。
        for gate,need,min_v in ((0.6,2,15.0),(0.15,3,4.0)):
            nz=d[np.abs(d)>gate]
            if len(nz)<need or (np.any(nz>0) and np.any(nz<0)):continue
            ts=a[:,0]-a[-1,0];centered=ts-ts.mean();den=float(centered@centered)
            if den<1e-8:continue
            v=float(centered@(a[:,1]-a[:,1].mean())/den)
            residual=float(np.sqrt(np.mean((a[:,1]-(a[:,1].mean()+v*centered))**2)))
            if min_v<=abs(v)<1200 and residual<=max(3.5,abs(v)*.08):return v
        return None

class Controller:
    def __init__(self,max_casts=3,jitter=False,latency_ms=35,seed=None,backend='sendinput',edge='both',bait_on=None):
        self.max_casts=int(max_casts);self.jitter=jitter;self.latency=float(latency_ms)/1000
        self.rng=random.Random(seed);self.backend=backend;self.edge=edge
        self.bait_on=bait_on          # threading.Event：撒鱼饵模式开关（可随时切换）
        self.bait_done=False          # 等级满/鱼饵耗尽后置位，本轮会话不再撒饵
        self.bait_prev_level=None;self.bait_prev_count=None
        self.bait_wait_since=None;self.bait_stop_seen=0
        self.bait_fails=0;self.bait_last_fail=0.  # 数字识别失败重试计数
        self.bait_flow_fails=0;self.bait_flow_cooldown=0.  # 撒饵流程失败冷却
        self.cast_count=0;self.cast_attempts=0;self.qte_count=0;self.collect_count=0
        self.state='';self.state_since=0.;self.started=None;self.last_progress=0.
        self.pending=None;self.pending_time=0.;self.pending_retries=0
        self.confirm_since=None;self.events=[];self.delay=.4
        self.motion=Motion();self.predicted=None
        self.qte_active=False;self.qte_since=None;self.qte_locked=False;self.qte_ack=False
        self.qte_time=-999.;self.clicked_bounds=None;self.left_target=False
        self.aim_dt=0.0;self.aim_hits=0          # 按压时机校正量（秒），命中后在线学习
        self.last_aim_k=None;self.last_aim_v=None # 最近一次窄区点击的搜索偏移/速度
        self.ack_by_flash=False                   # 本次锁定是否由闪光确认（可靠命中信号）
        self.nudge_idx=0                          # 同区重试时机偏移搜索步进
        self.flash_last=False;self.flash_end=-999.;self.changed_since=None
        self.qte_hold_ms=45
        self.hook_input_count=0;self.hook_followups=0;self.hook_unverified=0
        self.hook_window=False;self.hook_followed=False;self.hook_cycle_inputs=0
        self.hook_first_seen=None;self.hook_last_release=None;self.hook_fraction=None
        self.hook_live_since=None
        self.unknown_since=None;self.recovery_count=0;self.recovery_time=-999.
        self.recovery_sent=False
        self.qte_total=0
        self.qte_slow_since=None
        self.qte_last_fire=None   # 最近一次 QTE 点击（含兜底）发出时刻，看门狗基准
        self.qte_no_feedback_count=0  # 连续"点击后无反馈"宽限解锁次数（有界防死循环）
        lo,hi=min(MISS_RANGE),max(MISS_RANGE)
        self.qte_next_miss=self.rng.randint(lo,hi)
        self.qte_miss_pending=False

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
        if action.kind=='QTE':
            self.qte_total+=1
            if self.qte_total>=self.qte_next_miss:
                lo,hi=min(MISS_RANGE),max(MISS_RANGE)
                self.qte_next_miss=self.qte_total+self.rng.randint(lo,hi)
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

    def withdraw_bait(self,dryrun=False):
        """撤销未发出的撒饵点击。dryrun=True 时视为功能验证完成，不再重试。"""
        if self.pending in ('BAIT','BAIT2'):self.pending=None;self.confirm_since=None
        if dryrun:self.bait_done=True
        self.event('bait_dryrun' if dryrun else 'bait_skipped')

    def _bait_failed(self,t):
        """撒饵流程失败（弹窗未出现/打窝未生效）：绝不停止钓鱼会话。
        跳过本次，冷却 30 秒后自动重试；连续失败 3 次才放弃本会话撒饵。"""
        self.pending=None;self.confirm_since=None
        self.bait_flow_fails+=1
        self.last_progress=t
        if self.bait_flow_fails>=3:
            self.bait_done=True
            self.event('bait_done',reason='flow_failed')
        else:
            self.bait_flow_cooldown=t+30.
        self.event('bait_failed',fails=self.bait_flow_fails)
        return None

    def reset_qte(self):
        self.qte_active=False;self.qte_since=None;self.qte_locked=False;self.qte_ack=False
        self.motion.reset();self.changed_since=None;self.flash_last=False
        self.qte_miss_pending=False;self.qte_slow_since=None;self.qte_last_fire=None
        self.qte_no_feedback_count=0
    def abort_pending(self):
        """暂停结束/恢复时调用：在途输入已过时，作废并复位计时，避免立即超时停止。"""
        if self.pending:
            self.event('paused_drop',kind=self.pending)
            self.pending=None;self.confirm_since=None
        self.reset_qte();self.unknown_since=None
    def _pending_step(self,o,t,same_counter):
        kind=self.pending
        if kind in ('BAIT','BAIT2'):
            # 撒饵两段式：BAIT=点鱼饵包（弹出确认框）→ BAIT2=点绿色"打窝"按钮。
            # 真正撒饵发生在打窝点击后：以鱼饵计数减少或等级增长确认。
            changed=(o.bait_count is not None and self.bait_prev_count is not None
                     and o.bait_count<self.bait_prev_count) or \
                    (o.level is not None and self.bait_prev_level is not None
                     and o.level>self.bait_prev_level)
            if kind=='BAIT':
                if o.bait_popup and o.control:
                    self.event('bait_popup_seen',level=o.level,bait=o.bait_count)
                    return self.act('BAIT2',t,point=o.control)
                elapsed=t-self.pending_time
                if elapsed>4.5:
                    if self.pending_retries<1:
                        self.event('retry',kind='BAIT')
                        return self.act('BAIT',t,retry=True,reason='no_popup_retry',point=BAIT_POINT)
                    return self._bait_failed(t)
                return None
            # BAIT2：等待撒饵生效
            if changed:
                self.event('bait_spread',level=o.level,bait=o.bait_count)
                self.pending=None;self.confirm_since=None;self.last_progress=t
                self.bait_flow_fails=0
                return None
            elapsed=t-self.pending_time
            if elapsed>4.5:
                if self.pending_retries<1 and o.bait_popup and o.control:
                    self.event('retry',kind='BAIT2')
                    return self.act('BAIT2',t,retry=True,reason='bounded_retry',point=o.control)
                return self._bait_failed(t)
            return None
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
            self.state=o.state;self.state_since=t;self.bait_wait_since=None
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
        # 撒鱼饵开关关闭时，撤销在途未确认的撒饵点击，避免卡在等待确认。
        if self.pending in ('BAIT','BAIT2') and not (self.bait_on is not None and self.bait_on.is_set()):
            self.withdraw_bait()
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
            # 打窝等级会随时间衰减（实测 8 级约 10 分钟后掉回 4 级），bait_done
            # 不是永久状态：每竿回待机时若读到干净数字显示等级<8 且鱼饵>0，
            # 重新激活撒饵循环（同时也纠正单帧数字误读导致的错误关闭）。
            if (self.bait_done and self.bait_on is not None and self.bait_on.is_set()
                    and o.level is not None and o.bait_count is not None
                    and o.level<BAIT_LEVEL_MAX and o.bait_count>0
                    and t>=self.bait_flow_cooldown):
                self.bait_done=False;self.bait_stop_seen=0;self.bait_fails=0
                self.event('bait_reactivate',level=o.level,bait=o.bait_count)
            # 撒鱼饵模式：等级未满 8 且鱼饵包数量 > 0 时先撒饵；鱼饵为 0 直接钓鱼。
            if (self.bait_on is not None and self.bait_on.is_set()
                    and not self.bait_done and t>=self.bait_flow_cooldown):
                if o.level is None or o.bait_count is None:
                    # 待机动画/过渡帧常导致数字识别失败：短暂等待识别稳定。
                    # 失败后不永久放弃（星星闪光可能长期干扰），改为每 20 秒
                    # 重试一次；累计 5 次全部失败才放弃（识别大概率真坏了）。
                    if self.bait_wait_since is not None:
                        if t-self.bait_wait_since < 2.5:
                            return None
                        self.bait_wait_since=None
                        self.bait_fails += 1
                        self.bait_last_fail=t
                        if self.bait_fails >= 5:
                            self.bait_done=True
                            self.event('bait_done',reason='recognition_timeout')
                    elif (self.bait_fails == 0
                          or (t-self.bait_last_fail > 20 and self.bait_fails < 5)):
                        self.bait_wait_since=t
                        return None
                else:
                    self.bait_wait_since=None
                    self.bait_fails=0
                    full=o.level>=BAIT_LEVEL_MAX
                    empty=o.bait_count<=0
                    if full or empty:
                        # 二次确认：连续两个待机帧都满足才暂时禁用撒饵（等级衰减
                        # 或补充鱼饵后由上方 bait_reactivate 重新激活），
                        # 防止单帧数字误读（如 3 误读成 8）错误关闭功能。
                        self.bait_stop_seen+=1
                        reason='level_max' if full else 'no_bait'
                        if self.bait_stop_seen>=2:
                            self.bait_done=True
                            self.event('bait_done',reason=reason,
                                       level=o.level,bait=o.bait_count)
                        else:
                            self.event('bait_stop_pending',reason=reason,
                                       level=o.level,bait=o.bait_count)
                        # 未确认前先正常钓鱼，下个待机帧继续复核。
                    else:
                        self.bait_stop_seen=0
                        if o.bait_popup and o.control:
                            # 上次点鱼饵包的弹窗还开着：直接点"打窝"补完撒饵。
                            self.bait_prev_level=o.level;self.bait_prev_count=o.bait_count
                            self.last_progress=t
                            return self.act('BAIT2',t,point=o.control)
                        if t-self.state_since<self.delay:return None
                        self.last_progress=t
                        self.bait_prev_level=o.level;self.bait_prev_count=o.bait_count
                        return self.act('BAIT',t,point=BAIT_POINT)
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
            if self.qte_locked and t-self.qte_time>.025:
                self.qte_ack=True;self.ack_by_flash=True
            self.flash_last=True;self.motion.reset();self.predicted=None
            self.qte_slow_since=None
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
                # 命中确认：点击后 1.6s 内游戏给出反馈。闪光是最可靠的命中信号；
                # 黄区切换需 >15px（识别抖动 ±9px 会造成假"新黄区"，不能学）。
                # 把本次搜索偏移按当时速度折算成时间校正量，EMA 收敛到 aim_dt，
                # 之后每个黄区第一 press 就直接用校准后的时机，不再从头搜索。
                shifted=self.clicked_bounds and o.target and (
                    abs(o.target[0]-self.clicked_bounds[0])>15 or
                    abs(o.target[1]-self.clicked_bounds[1])>15)
                if (not self.qte_miss_pending and self.last_aim_k
                        and self.last_aim_v and t-self.qte_time<=1.6
                        and (self.ack_by_flash or shifted)):
                    dt=self.aim_dt+0.45*self.last_aim_k/self.last_aim_v
                    self.aim_dt=max(-0.30,min(0.30,dt))
                    self.aim_hits+=1
                    self.event('aim_calibrated',k=self.last_aim_k,
                               dt=round(self.aim_dt,3),hits=self.aim_hits,
                               via='flash' if self.ack_by_flash else 'shift')
                self.ack_by_flash=False
                self.qte_locked=False;self.qte_ack=False;self.left_target=False
                self.changed_since=None;self.motion.reset();self.last_progress=t
                self.qte_miss_pending=False;self.qte_last_fire=t
                self.qte_no_feedback_count=0
            else:
                if (not self.qte_ack and self.qte_miss_pending and t-self.qte_time>=.9
                        and not o.flash):
                    # 失误点击后游戏可能不结束本轮（同一黄区继续）。
                    # 宽限结束后直接解锁，继续正常瞄准；失误本身已计入。
                    self.qte_locked=False;self.qte_ack=False;self.left_target=False
                    self.changed_since=None;self.motion.reset();self.last_progress=t
                    self.qte_miss_pending=False;self.qte_last_fire=t
                    self.event('qte_miss_grace_unlock')
                elif not self.qte_ack and t-self.qte_time>(
                        1.1 if (o.target is not None and o.x is not None) else 3.2):
                    # 点击后长时间无反馈不一定是识别坏了：普通点击落空（游标
                    # 没进黄区）时游戏同样不结束本轮；大鱼还可能是同一黄区
                    # 需要多次命中的重复轮。游戏 QTE 有倒计时，本轮必然结束。
                    # 识别健康（黄区+游标都在）时只解锁继续瞄准、不计入停止
                    # 计数；识别退化（看不到黄区或游标）才累计，连续 2 次仍
                    # 无反馈才判定画面异常并停止会话。
                    # 实测（2026-11-05）：本游戏版本的命中闪光检测不到
                    # （flash_fraction 始终 <0.07，阈值 0.20），旧版固定等
                    # 3.2s 才解锁，点击间隔被拉到 3~5s，大鱼倒计时内点不满
                    # 次数就脱钩。识别健康时降到 1.1s：游标还在黄区里就尽快
                    # 补点；识别退化时仍保持 3.2s，避免误判停机。
                    healthy=o.target is not None and o.x is not None
                    if healthy:
                        self.qte_locked=False;self.qte_ack=False;self.left_target=False
                        self.changed_since=None;self.last_progress=t
                        self.qte_miss_pending=False;self.qte_last_fire=t
                        self.qte_no_feedback_count=0
                        # 同区重试不清运动历史：游标运动连续，清掉后速度估计
                        # 要重新积累 3 帧才可用，会白白错过 1~2 次发射窗口。
                        self.event('qte_zone_repeat_unlock')
                    elif self.qte_no_feedback_count<2:
                        self.qte_no_feedback_count+=1
                        self.qte_locked=False;self.qte_ack=False;self.left_target=False
                        self.changed_since=None;self.motion.reset();self.last_progress=t
                        self.qte_miss_pending=False;self.qte_last_fire=t
                        self.event('qte_no_feedback_unlock',
                                   count=self.qte_no_feedback_count)
                    else:
                        return self.stop('qte_no_feedback')
                return None
        # QTE 僵死看门狗：无论何种原因（识别全失败/速度异常/失误窗口空转），
        # 只要本轮 QTE 太久没有任何点击就强制出手，避免干等到 qte_timeout 丢鱼。
        since=self.qte_last_fire if self.qte_last_fire is not None else self.qte_since
        if (since is not None and not self.qte_locked
                and t-since>QTE_FALLBACK_IN and t-self.flash_end>.08):
            inside_now=(o.x is not None and o.target is not None
                        and o.target[0]<=o.x<=o.target[1])
            if inside_now or t-since>QTE_FALLBACK_BLIND:
                tier='inside' if inside_now else 'blind'
                self.qte_locked=True;self.qte_ack=False;self.left_target=False
                self.qte_time=t;self.clicked_bounds=o.target;self.changed_since=None
                self.last_progress=t;self.qte_miss_pending=True
                self.qte_last_fire=t
                self.event('qte_watchdog',tier=tier,elapsed=round(t-since,1))
                return Action('QTE',(295,858),
                              'watchdog(%s) elapsed=%.1fs'%(tier,t-since),
                              False,self.qte_hold_ms,self.backend)
        if o.x is None or o.target is None:return None
        v=self.motion.velocity()
        if v is None:
            # 慢速漂移轮兜底：速度估计持续不可用（游戏慢速轮浮标近乎静止，
            # 帧位移低于噪声门限），但浮标已稳定停在黄区内 → 直接点击，无需预测。
            if self.qte_slow_since is None:self.qte_slow_since=t
            if (not self.qte_locked and t-self.qte_slow_since>=1.2
                    and t-self.flash_end>.08 and not self.qte_miss_pending):
                l,r=o.target;margin=max(5.0,(r-l)*.12)
                if l+margin<=o.x<=r-margin:
                    self.qte_locked=True;self.qte_ack=False;self.left_target=False
                    self.qte_time=t;self.clicked_bounds=o.target;self.changed_since=None
                    self.last_progress=t;self.predicted=o.x;self.qte_last_fire=t
                    return Action('QTE',(295,858),
                                  'slow_drift x=%.1f target=%.1f..%.1f hold=%dms'%(o.x,l,r,self.qte_hold_ms),
                                  False,self.qte_hold_ms,self.backend)
            return None
        self.qte_slow_since=None
        l0,r0=o.target;width=r0-l0
        if width<9:return None
        margin=max(2.5,min(10,width*.14))
        # aim_dt：全局按压时机校正（秒）。QTE 触点是固定屏幕位置，游戏判定的是
        # 按压时刻鱼是否在黄区内——位置瞄准无意义，本质是时机问题。三实例并行
        # 时帧龄+触控传输延迟会让时机系统性偏移，宽区容错大看不出来，窄区必丢。
        # 校正值由命中反馈在线学习（见 qte_ack 解锁处），随会话自适应。
        delay=self.latency+max(0,min(.12,processing_delay))+self.aim_dt
        down=o.x+v*delay;up=down+v*self.qte_hold_ms/1000
        self.predicted=up
        inside=lambda x:l0+margin<=x<=r0-margin
        # 同一黄区点击后无反馈（大概率未命中）→ 按压时机偏移搜索：
        # 0→+12→-12→+26→-26→+45→-45（px 等效，±45px≈±0.22s@200px/s），
        # 换新黄区后从全局校准值重新开始。
        same=(self.clicked_bounds is not None
              and abs(self.clicked_bounds[0]-l0)<6 and abs(self.clicked_bounds[1]-r0)<6)
        seq=(0,12,-12,26,-26,45,-45)
        idx=self.nudge_idx if same else 0
        k=seq[min(idx,len(seq)-1)]
        if width<65:
            # 窄区：等预测按压中点越过（区中心+搜索偏移）即出手。不做 inside()
            # 硬约束——触点位置固定，按下时扫描段超出视觉区不影响游戏判定，
            # 且硬约束会把发射窗口压到 ~40ms，帧率不足时永远等不到（实测
            # 看门狗暴涨）。穿越后 25px 容错带内都允许出手，错过精确穿越点
            # 也能补上；预测位置离区太远说明这帧不该出手，等折返。
            mid=down+v*self.qte_hold_ms/2000
            c=(l0+r0)/2+k
            if v>0:eligible=c-6<=mid<=c+25
            else:eligible=c-25<=mid<=c+6
        elif self.edge=='down':eligible=inside(down)
        elif self.edge=='up':eligible=inside(up)
        else:
            # Keep both edges in yellow when possible. For very narrow targets,
            # use release timing explicitly rather than adding a random QTE wait.
            both_possible=width-2*margin>abs(v)*self.qte_hold_ms/1000+3
            eligible=inside(down) and inside(up) if both_possible else inside(up)
        miss_due=self.qte_total+1>=self.qte_next_miss
        if miss_due and self.rng.random()<.5:
            # 抢拍式失误：游标尚在黄区前方 6~30px 时提前按下，且松开仍在区外。
            early=(v>0 and l0-30<=down<=l0-6 and up<=l0-4) or \
                  (v<0 and r0+6<=down<=r0+30 and up>=r0+4)
            if early:
                detail='x=%.1f speed=%.1f down=%.1f up=%.1f target=%.1f..%.1f'%(o.x,v,down,up,l0,r0)
                return self._fire_qte(t,o.target,'deliberate_miss(early) '+detail,self.qte_hold_ms)
        if eligible:
            if miss_due:
                # 松晚式失误：按下时机正常，按住时间拖长到游标滑出黄区远端。
                overshoot=self.rng.uniform(4,14)
                up_goal=(r0+margin+overshoot) if v>0 else (l0-margin-overshoot)
                hold=(up_goal-down)/v*1000
                if 60<=hold<=240:
                    hold=int(hold)
                    self.predicted=down+v*hold/1000
                    detail='x=%.1f speed=%.1f down=%.1f up_goal=%.1f target=%.1f..%.1f'%(o.x,v,down,up_goal,l0,r0)
                    return self._fire_qte(t,o.target,'deliberate_miss(late) '+detail,hold)
                # 时机未到（按住时长窗口外）：等下一帧，不发正常点击，
                # 否则本轮会被普通点击消耗掉，失误永远排不上。
                return None
            self.qte_locked=True;self.qte_ack=False;self.left_target=False
            self.qte_time=t;self.clicked_bounds=o.target;self.changed_since=None;self.last_progress=t
            self.qte_last_fire=t
            if width<65:
                # 记录本次搜索偏移：若 1.6s 内游戏确认（闪光/黄区切换），
                # 在 qte_ack 解锁处把 k/v 折算成时间校正量存入 aim_dt。
                self.last_aim_k=k;self.last_aim_v=abs(v);self.ack_by_flash=False
                if same:self.nudge_idx=min(self.nudge_idx+1,len(seq)-1)
                else:self.nudge_idx=1
            else:
                self.last_aim_k=None
            return Action('QTE',(295,858),
                          'x=%.1f speed=%.1f down=%.1f up=%.1f target=%.1f..%.1f k=%+d dt=%+.2f'%(o.x,v,down,up,l0,r0,k,self.aim_dt),
                          False,self.qte_hold_ms,self.backend)
        return None

    def _fire_qte(self,t,target,reason,hold_ms):
        """Fire a deliberate-miss QTE tap with the standard round lock + grace flag."""
        self.qte_locked=True;self.qte_ack=False;self.left_target=False
        self.qte_time=t;self.clicked_bounds=target;self.changed_since=None;self.last_progress=t
        self.qte_miss_pending=True;self.qte_last_fire=t
        self.event('qte_deliberate_miss',style=reason,hold_ms=hold_ms)
        return Action('QTE',(295,858),reason,False,hold_ms,self.backend)
