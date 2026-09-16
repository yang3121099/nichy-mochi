"""Shared-folder controls and an automatically updated, plain-file conversation."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import mochi_core as core


def read_or(path, default):
    try:
        value=core.read_json(path)
        return value if default is None or isinstance(value,type(default)) else default
    except (OSError, ValueError):
        return default


def create_once(path, data):
    if path.exists():
        return
    temp = path.with_name('.initial-'+uuid.uuid4().hex)
    try:
        core.atomic_write(temp,data)
        try:
            os.link(temp,path)
            core.sync_dir(path.parent)
        except FileExistsError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def prepare(root):
    # Files are initialized once. Existing commands, logs and heartbeat code stay intact.
    create_once(root/'command.sh', b'# Write your launch commands here, then touch RUN.\necho "Hello, Mochi"\n')
    create_once(root/'keep_alive.py', Path(__file__).with_name('keep_alive.py').read_bytes())
    create_once(root/'log',b'')


class MeetingPoint:
    def __init__(self, worker):
        self.worker, self.root = worker, worker.root
        self.signals = read_or(self.root/'.signals.json', {})
        self.views = read_or(self.root/'.views.json', {})
        self.last_poll = 0.0
        self.last_view = 0.0
        self.last_status = None

    def say(self, text):
        with (self.root/'log').open('ab') as stream:
            stream.write(('\n['+time.strftime('%H:%M:%S')+'] '+text+'\n').encode())

    def signal(self, name):
        path = self.root/name
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if not path.is_file() or path.is_symlink():
            return None
        # Keep the marker in place: a new touch arriving during handling is not deleted.
        fingerprint = hashlib.sha256(repr((info.st_ino,info.st_mtime_ns,info.st_size)).encode()).hexdigest()
        return fingerprint if self.signals.get(name,{}).get('fingerprint') != fingerprint else None

    def poll(self):
        if time.monotonic()-self.last_poll < .5:
            return
        self.last_poll = time.monotonic()
        for name in ['STOP','RUN']:
            fingerprint = self.signal(name)
            if fingerprint is None:
                continue
            record = dict(fingerprint=fingerprint, at=time.time())
            try:
                if name == 'RUN':
                    job_id = 'file-'+fingerprint[:24]
                    job = self.root/'jobs'/job_id
                    if not job.exists():
                        body = (self.root/'command.sh').read_bytes()
                        if len(body)>1024*1024:
                            raise ValueError('command.sh 超过 1 MiB')
                        with tempfile.TemporaryDirectory(prefix='mochi-command-') as temp:
                            script = Path(temp)/'command.sh'
                            script.write_bytes(body)
                            checked = subprocess.run(['bash','-n',str(script)],capture_output=True,text=True,timeout=5)
                            if checked.returncode:
                                raise ValueError('command.sh 的 shell 语法需要检查')
                            args = argparse.Namespace(script=str(script),id=job_id,
                                cwd=os.environ.get('NICHY_CODE_DIR',str(self.root.parent)),timeout=86400,
                                source=None,snapshot_limit_mib=100,background=False,resume_safe=False,
                                max_preemptions=0,label='command.sh')
                            with contextlib.redirect_stdout(io.StringIO()):
                                core.submit(self.root,args)
                    record.update(state='QUEUED',job=job_id)
                else:
                    from nichy import stop_task
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        stop_task(self.root,argparse.Namespace(id=None))
                    self.say(output.getvalue().strip())
                    record['state'] = 'APPLIED'
            except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
                record.update(state='REJECTED',error=str(exc))
                self.say('Mochi 没有接下 '+name+'：'+str(exc))
            self.signals[name] = record
            core.write_json(self.root/'.signals.json', self.signals)

    def update(self, force=False):
        if not force and time.monotonic()-self.last_view<.5:
            return
        self.last_view = time.monotonic()
        from nichy import describe, label, show_status, latest
        changed = False
        for job in core.jobs(self.root):
            spec = core.read_json(job/'request.json')
            state = core.read_json(job/'status.json')['state']
            old = self.views.setdefault(job.name,dict(offset=0))
            receipt = read_or(job/'received.json',{})
            if receipt.get('state')=='READ' and not old.get('received'):
                self.say('Mochi 收到了：'+label(spec))
                old['received'] = True
                changed = True
            if state == 'RUNNING' and old.get('state') != state:
                self.say('Mochi 正在运行：'+label(spec))
                old['state'] = state
                changed = True
            log = job/'run.log'
            at_end = True
            if log.exists():
                size = log.stat().st_size
                if old['offset']>size:
                    old['offset']=0
                with log.open('rb') as source:
                    source.seek(old['offset'])
                    # Bound log mirroring so a noisy task cannot starve supervision.
                    data = source.read(256*1024)
                if data:
                    self.say('输出 · '+label(spec))
                    with (self.root/'log').open('ab') as target:
                        target.write(data)
                    old['offset'] += len(data)
                    changed = True
                at_end = old['offset']>=size
            if state in core.TERMINAL and at_end and old.get('state') != state:
                self.say(describe(job))
                old['state'] = state
                changed = True
        if changed:
            core.write_json(self.root/'.views.json',self.views)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            show_status(self.root,argparse.Namespace(json=False))
        beat = read_or(self.root/'worker.json',{}).get('updated_at',time.time())
        status = output.getvalue()+'更新：'+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(beat))+'\n'
        rejected = self.signals.get('RUN',{})
        if rejected.get('state')=='REJECTED':
            status += '最近 RUN 未接收：'+rejected['error']+'\n'
        if status != self.last_status:
            core.atomic_write(self.root/'status.txt',status.encode())
            self.last_status = status
        newest = latest(self.root)
        if newest:
            spec = core.read_json(newest/'request.json')
            receipt = dict(job=newest.name,label=spec['label'],revision=spec['revision'],
                state=core.read_json(newest/'status.json')['state'],
                received=read_or(newest/'received.json',{}))
            if receipt != read_or(self.root/'receipt.json',None):
                core.write_json(self.root/'receipt.json',receipt)
