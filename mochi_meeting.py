"""Shared-folder controls and an automatically updated, plain-file conversation."""
import argparse
import contextlib
import csv
from datetime import datetime
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


def timestamp(at=None):
    value = datetime.now().astimezone() if at is None else datetime.fromtimestamp(at).astimezone()
    return value.isoformat(sep=' ', timespec='seconds')


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


def file_fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    import stat
    if not stat.S_ISREG(info.st_mode):
        return None
    return hashlib.sha256(repr((info.st_ino,info.st_mtime_ns,info.st_ctime_ns,info.st_size)).encode()).hexdigest()


def prepare(root):
    # Files are initialized once. Existing commands, logs and heartbeat code stay intact.
    create_once(root/'command.sh', b'# Write your launch commands here, save this file last to submit.\necho "Hello, Mochi"\n')
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
        self.command_candidate = None
        self.submissions = None
        self.index_dirty = True
        # First installation treats the existing template as a baseline. Later
        # starts retain the last handled fingerprint, including offline uploads.
        if 'command.sh' not in self.signals:
            self.remember('command.sh',dict(fingerprint=file_fingerprint(self.root/'command.sh'),state='BASELINE'))

    def say(self, text):
        with (self.root/'log').open('ab') as stream:
            stream.write(('\n['+timestamp()+'] '+text+'\n').encode())

    def signal(self, name):
        fingerprint = file_fingerprint(self.root/name)
        return fingerprint if self.signals.get(name,{}).get('fingerprint') != fingerprint else None

    def remember(self, name, record):
        self.signals[name] = record
        core.write_json(self.root/'.signals.json', self.signals)

    def publish_command(self, fingerprint, explicit=False):
        job_id = ('file-' if explicit else 'command-')+fingerprint[:24]
        if not (self.root/'jobs'/job_id).exists():
            with (self.root/'command.sh').open('rb') as source:
                body = source.read(1024*1024+1)
            if len(body)>1024*1024:
                raise ValueError('command.sh 超过 1 MiB')
            if not explicit and file_fingerprint(self.root/'command.sh') != fingerprint:
                return None  # A new upload arrived while reading; wait for it to settle.
            if not any(line.strip() and not line.lstrip().startswith(b'#') for line in body.splitlines()):
                raise ValueError('command.sh 还没有启动指令')
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
        return job_id

    def poll(self):
        if time.monotonic()-self.last_poll < .5:
            return
        self.last_poll = time.monotonic()
        for name in ['STOP','RUN']:
            fingerprint = self.signal(name)
            if fingerprint is None:
                continue
            record = dict(fingerprint=fingerprint, at=time.time())
            command_fingerprint = file_fingerprint(self.root/'command.sh')
            try:
                if name == 'RUN':
                    record.update(state='QUEUED',job=self.publish_command(fingerprint,explicit=True))
                else:
                    from nichy import stop_task
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        stop_task(self.root,argparse.Namespace(id=None))
                    for line in output.getvalue().splitlines():
                        self.say(line)
                    record['state'] = 'APPLIED'
            except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
                record.update(state='REJECTED',error=str(exc))
                self.say('Mochi 没有接下 '+name+'：'+str(exc))
            if name == 'RUN':
                # The explicit marker covers this upload; do not also auto-submit it.
                self.signals['command.sh'] = dict(record,fingerprint=command_fingerprint)
            self.remember(name,record)
        self.poll_command()

    def poll_command(self):
        fingerprint = self.signal('command.sh')
        if fingerprint is None:
            self.command_candidate = None
            return
        if not self.command_candidate or self.command_candidate[0] != fingerprint:
            self.command_candidate = (fingerprint,time.monotonic())
            return
        if time.monotonic()-self.command_candidate[1] < 2:
            return
        record = dict(fingerprint=fingerprint,at=time.time())
        try:
            job = self.publish_command(fingerprint)
            if job is None:
                return
            record.update(state='QUEUED',job=job)
        except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
            record.update(state='REJECTED',error=str(exc))
            self.say('Mochi 没有接下 command.sh：'+str(exc))
        self.remember('command.sh',record)
        self.command_candidate = None

    def register_submissions(self):
        registry = self.root/'.submissions.json'
        if self.submissions is None:
            loaded = core.read_json(registry) if registry.exists() else {}
            if not isinstance(loaded,dict) or any(not isinstance(record,dict) for record in loaded.values()):
                raise ValueError('提交编号索引需要检查')
            self.submissions = loaded
        records = list(self.submissions.values())
        numbers = [record['number'] for record in records]
        if any(type(number) is not int or number < 1 for number in numbers) or len(numbers) != len(set(numbers)):
            raise ValueError('提交编号索引需要检查')
        next_number = max(numbers,default=0)+1
        changed = False
        updated = dict(self.submissions)
        pending = [(core.read_json(job/'request.json'),job) for job in core.jobs(self.root) if job.name not in self.submissions]
        for spec,job in sorted(pending,key=lambda item:(item[0]['submitted_at'],item[1].name)):
            updated[job.name] = dict(number=next_number,label=spec['label'],revision=spec['revision'],
                                             submitted_at=spec['submitted_at'],job=str(job))
            next_number += 1
            changed = True
        if changed:
            # Persist identities before displaying them. Keep entries after job
            # archival so a later submission never reuses a published number.
            core.write_json(registry,updated)
            self.submissions = updated
            self.index_dirty = True
        if self.index_dirty or not (self.root/'submissions.tsv').exists():
            output = io.StringIO()
            writer = csv.writer(output,delimiter='\t',lineterminator='\n')
            writer.writerow(['提交','提交时间','指令','任务目录','完整版本'])
            for record in sorted(self.submissions.values(),key=lambda row:row['number']):
                writer.writerow(['第 {} 次提交'.format(record['number']),timestamp(record['submitted_at']),
                                 record['label'],record['job'],record['revision']])
            core.atomic_write(self.root/'submissions.tsv',output.getvalue().encode())
            self.index_dirty = False

    def update(self, force=False):
        if not force and time.monotonic()-self.last_view<.5:
            return
        self.last_view = time.monotonic()
        from nichy import describe, label, show_status, latest
        self.register_submissions()
        changed = False
        for job in core.jobs(self.root):
            if job.name not in self.submissions:
                continue  # Registered on the next cycle if published during this update.
            spec = core.read_json(job/'request.json')
            state = core.read_json(job/'status.json')['state']
            old = self.views.setdefault(job.name,dict(offset=0))
            if not old.get('submitted'):
                if not old.get('received') and 'state' not in old:
                    self.say('Nichy 提交了：'+label(spec,job))
                old['submitted'] = True
                changed = True
            receipt = read_or(job/'received.json',{})
            if receipt.get('state')=='READ' and not old.get('received'):
                self.say('Mochi 收到了：'+label(spec,job))
                old['received'] = True
                changed = True
            if state == 'RUNNING' and old.get('state') != state:
                self.say('Mochi 正在运行：'+label(spec,job))
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
                    self.say('Mochi 输出：'+label(spec,job))
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
        status = output.getvalue()+'更新：'+timestamp(beat)+'\n'
        for name in ['command.sh','RUN']:
            rejected = self.signals.get(name,{})
            if rejected.get('state')=='REJECTED':
                status += '最近 '+name+' 未接收：'+rejected['error']+'\n'
        if status != self.last_status:
            core.atomic_write(self.root/'status.txt',status.encode())
            self.last_status = status
        newest = latest(self.root)
        if newest and newest.name in self.submissions:
            spec = core.read_json(newest/'request.json')
            receipt = dict(job=newest.name,label=spec['label'],revision=spec['revision'],
                submission=self.submissions[newest.name]['number'],
                state=core.read_json(newest/'status.json')['state'],
                received=read_or(newest/'received.json',{}))
            if receipt != read_or(self.root/'receipt.json',None):
                core.write_json(self.root/'receipt.json',receipt)

# Codex（OpenAI）贡献：文件交接与自动回执、稳定提交编号、日志交互及隔离测试。
