"""Queue worker plus a quiet, preemptible, visible-GPU-only heartbeat."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid
from mochi_core import Worker, write_json, atomic_write, set_state
from keep_alive import gpu_stats, eligible
from mochi_meeting import MeetingPoint, timestamp

DEFAULTS = dict(enabled=True, interval=1800, seconds=10, idle_for=30, duty=.25)

def checked_config(config):
        if not isinstance(config,dict) or set(config)-set(DEFAULTS):
            raise ValueError('unknown heartbeat setting')
        c=dict(DEFAULTS, **config)
        if not isinstance(c['enabled'], bool):
            raise ValueError('heartbeat enabled must be true or false')
        for k in ['interval','seconds','idle_for','duty']:
            if not isinstance(c[k],(int,float)) or not __import__('math').isfinite(c[k]):
                raise ValueError('invalid heartbeat setting: '+k)
        if c['interval'] < 1 or not 0<c['seconds']<=120 or c['idle_for']<0 or not .01<=c['duty']<=.25:
            raise ValueError('invalid heartbeat timing or duty')
        return c

class NichyWorker(Worker):
    def __init__(self, root, args, config=None):
        super().__init__(root,args)
        self.config_error=None
        self.config_reported=False
        self.pulse_config=checked_config(config if config is not None else {})
        if config is None:
            try:
                self.pulse_config=checked_config(json.loads((root/'config.json').read_text()))
            except FileNotFoundError:
                pass
            except (OSError,ValueError,TypeError) as exc:
                self.config_error=str(exc)
        c=self.pulse_config
        self.pulse_next=time.monotonic()+c['idle_for']
        self.pulse_last_sample=0.0
        self.visible=[]
        self.quiet_samples=0
        self.quiet_devices=set()
        self.probe_next=0.0
        self.heartbeat_state='off' if not c['enabled'] else 'waiting'
        self.meeting=MeetingPoint(self)
        local_script=root/'keep_alive.py'
        self.pulse_script=(local_script if local_script.is_file() else Path(__file__).with_name('keep_alive.py')).resolve()
        self.last_display_error=None
        self.last_pulse_log=None

    def probe_visible(self):
        self.probe_next=time.monotonic()+60
        try:
            r=subprocess.run([sys.executable,str(self.pulse_script),'--probe'],
                             capture_output=True,text=True,check=True,timeout=15)
            self.visible=json.loads(r.stdout)
            if not self.visible:self.pulse_status('no-visible-gpu')
        except (OSError,ValueError,subprocess.SubprocessError):
            self.pulse_status('unavailable')

    def heartbeat(self,force=False):
        if self.state in {'IDLE','RUNNING'}:
            self.meeting.poll()
        if not self.config_reported:
            self.meeting.say('Mochi 已就位：'+str(self.root))
            self.pulse_status('config-error' if self.config_error else self.heartbeat_state,
                              error=self.config_error)
            self.config_reported=True
        super().heartbeat(force)
        try:
            self.meeting.update(force)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            # Display files are derived data; a broken display must not stop cleanup.
            detail=str(exc)
            if detail != self.last_display_error:
                print('Mochi 状态显示暂不可用：'+detail,flush=True)
                self.last_display_error=detail

    def pulse_status(self, state, **extra):
        self.heartbeat_state=state
        if state != self.last_pulse_log:
            messages={'waiting':'待命','running':'运行中','yielding':'让出 GPU',
                      'off':'已关闭','config-error':'配置需要检查','no-visible-gpu':'无可见 GPU',
                      'unavailable':'环境暂不可用','telemetry-unavailable':'指标暂不可用'}
            try:
                with (self.root/'keep_alive.log').open('a') as stream:
                    stream.write('['+timestamp()+'] Mochi 心跳 · '+messages.get(state,state)+'\n')
                self.last_pulse_log=state
            except OSError:
                pass  # A display-file failure must not prevent task cleanup.
        write_json(self.root/'keep_alive.json',dict(state=state,updated_at=time.time(),
                   config=self.pulse_config,**extra))

    def idle_job(self, idle_since):
        c=self.pulse_config
        if not c['enabled'] or self.config_error:
            return None
        if time.monotonic()<self.pulse_next or time.monotonic()-idle_since<c['idle_for']:
            return None
        if not self.visible and time.monotonic()>=self.probe_next:
            self.probe_visible()
        if not self.visible:
            return None
        if time.monotonic()-self.pulse_last_sample<1:
            return None
        self.pulse_last_sample=time.monotonic()
        try:
            candidates=eligible(gpu_stats(),self.visible)
        except (OSError,ValueError,subprocess.SubprocessError):
            self.quiet_samples=0
            self.pulse_next=time.monotonic()+30
            self.pulse_status('telemetry-unavailable')
            return None
        if not candidates:
            self.quiet_samples=0
            self.quiet_devices=set()
            self.pulse_next=time.monotonic()+30
            self.pulse_status('yielding')
            return None
        current={card['uuid'] for card in candidates}
        stable=current & self.quiet_devices
        self.quiet_devices=current
        if not stable:
            self.quiet_samples=0
        self.quiet_samples+=1
        if self.quiet_samples<2:
            return None
        # Recheck commands after the telemetry calls, before starting any GPU work.
        now,drain=self.flags()
        if self.pending() or now or drain or self.interrupted:
            return None
        self.quiet_samples=0
        self.pulse_next=time.monotonic()+c['interval']
        uid=min((card for card in candidates if card['uuid'] in stable),key=lambda x:x['utilization'])['uuid']
        parent=self.root/'pulses';parent.mkdir(exist_ok=True)
        job=parent/('pulse-'+uuid.uuid4().hex[:12]);job.mkdir(mode=0o700)
        body=('exec "$NICHY_PYTHON" '+shlex.quote(str(self.pulse_script))+' --uuid '+shlex.quote(uid)+
              ' --seconds '+str(c['seconds'])+' --duty '+str(c['duty'])+'\n').encode()
        spec=dict(id=job.name,label='GPU 心跳',pulse=True,pulse_gpu_uuid=uid,background=True,
                  resume_safe=False,timeout=c['seconds']+30,cwd=str(job),sha256=hashlib.sha256(body).hexdigest(),
                  submitted_at=time.time(),source=None,revision=hashlib.sha256(body).hexdigest())
        atomic_write(job/'run.sh',body);write_json(job/'request.json',spec);set_state(job,'PENDING')
        self.pulse_status('running',gpu=uid)
        # Pulse history is bounded; current and unresolved attempts are never removed.
        import shutil
        old=sorted(parent.iterdir(),key=lambda x:x.stat().st_mtime)
        for p in old[:-20]:
            try:
                if json.loads((p/'status.json').read_text()).get('state') in {'SUCCEEDED','FAILED','PREEMPTED','TIMED_OUT','CANCELLED','INTERRUPTED'}:
                    shutil.rmtree(p)
            except (OSError,ValueError):
                pass
        return job

    def execute(self, job):
        pulse=job.parent == self.root/'pulses'
        try:
            super().execute(job)
        finally:
            if pulse:self.pulse_status('config-error' if self.config_error else
                                      ('waiting' if self.pulse_config['enabled'] else 'off'))
