"""Continuous idle work, with one CUDA child per allocated device.

The controller never creates a CUDA context. Nichy's existing process guard owns
the entire tree and confirms cleanup before a real task may start.
"""
from collections import deque
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from keep_alive import gpu_stats, eligible, probe
from mochi_core import write_json


def next_duty(current, measured, target):
    if not all(math.isfinite(x) for x in (current, measured, target)):
        raise ValueError('invalid utilization sample')
    return max(.5, min(1.0, current + .25*(target-measured)/100))


def busy_reason(cards, uuids, owners):
    for uid in uuids:
        card = cards.get(uid)
        if card is None:
            return 'telemetry-unavailable'
        if card['free_percent'] <= 60:
            return 'memory-pressure'
        pids = card['processes']
        if len(pids)>1 or (uid in owners and pids != [owners[uid]]):
            return 'external-demand'
    # High utilization from our own children is expected, not external demand.
    return None


def stop_children(children):
    for child in children:
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
    until = time.monotonic()+1
    for child in children:
        try:
            child.wait(timeout=max(.01, until-time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
    for child in children:
        child.wait(timeout=3)
        if child.stdin:
            child.stdin.close()


def continuous(uuids, seconds, target, initial_duty=1):
    if not uuids or len(set(uuids))!=len(uuids) or os.environ.get('CUDA_VISIBLE_DEVICES','').split(',')!=uuids:
        raise ValueError('continuous heartbeat device mapping changed')
    job = Path(os.environ['GPUQ_JOB_DIR'])
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
    children, owners = [], {}
    samples = {uid:deque(maxlen=60) for uid in uuids}
    duties = {uid:initial_duty for uid in uuids}
    started = time.monotonic()
    ready = False
    status = 'complete'
    try:
        cards = gpu_stats()
        if len(eligible(cards,uuids)) != len(uuids):
            status = 'skipped-busy'
            return
        for slot,uid in enumerate(uuids):
            if stopping[0]:
                break
            env = dict(os.environ,CUDA_VISIBLE_DEVICES=uid,NICHY_LOAD_SLOT=str(slot))
            child = subprocess.Popen([sys.executable,str(Path(__file__).with_name('keep_alive.py')),
                '--load-worker','--uuid',uid],env=env,stdin=subprocess.PIPE,bufsize=0)
            children.append(child)
        while not stopping[0] and time.monotonic()-started<seconds:
            if len(children)!=len(uuids) or any(child.poll() is not None for child in children):
                status = 'child-failed'
                break
            cards = gpu_stats()
            reason = busy_reason(cards,uuids,owners)
            if reason:
                status = reason
                break
            all_ready = all((job/('load-%d-ready.json'%slot)).exists() for slot in range(len(uuids)))
            if all_ready and not ready:
                # NVML can report host PIDs. Learn them only once all CUDA
                # children are ready and each device reports exactly one process.
                if any(len(cards[uid]['processes'])!=1 for uid in uuids):
                    status = 'telemetry-unavailable'
                    break
                owners = {uid:cards[uid]['processes'][0] for uid in uuids}
                write_json(job/'pulse-ready.json',dict(pid=os.getpid(),pids=[p.pid for p in children],
                                                     uuids=uuids,at=time.time()))
                ready = True
            if not ready and time.monotonic()-started>60:
                status = 'initialization-timeout'
                break
            rows = []
            for uid,child in zip(uuids,children):
                if ready:
                    measured = cards[uid]['utilization']
                    samples[uid].append(measured)
                    duties[uid] = next_duty(duties[uid],measured,target)
                    rows.append(dict(uuid=uid,utilization=measured,duty=round(duties[uid],3),
                        average=round(sum(samples[uid])/len(samples[uid]),2),samples=len(samples[uid])))
                child.stdin.write(('%.5f\n'%duties[uid]).encode())
            if ready:
                write_json(job/'pulse-metrics.json',dict(at=time.time(),scope='idle-only-last-60-samples',
                    target=target,gpus=rows,average=round(sum(row['average'] for row in rows)/len(rows),2)))
            until = time.monotonic()+1
            while not stopping[0] and time.monotonic()<until:
                time.sleep(min(.05,max(0,until-time.monotonic())))
        if stopping[0]:
            status = 'yielded'
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        status = 'telemetry-or-child-error'
        write_json(job/'pulse-error.json',dict(error=str(exc),at=time.time()))
    finally:
        stop_children(children)
        write_json(job/'pulse-result.json',dict(status=status,uuids=uuids,seconds=time.monotonic()-started,
                                              checked=ready,children_stopped=True))
    if status in {'child-failed','initialization-timeout','telemetry-or-child-error'}:
        raise RuntimeError('idle load stopped: '+status)


def load_worker(expected):
    import torch
    if probe()!=[expected]:
        raise ValueError('idle load child device mapping changed')
    torch.set_num_threads(1)
    torch.backends.cuda.matmul.allow_tf32 = False
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0,True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0,True))
    size = 4096
    a = torch.ones((size,size),device='cuda',dtype=torch.float32)
    b,c = torch.ones_like(a),torch.empty_like(a)
    torch.mm(a,b,out=c)
    torch.cuda.synchronize()
    if c[0,0].item()!=size:
        raise ValueError('idle load numerical check failed')
    job = Path(os.environ['GPUQ_JOB_DIR'])
    slot = int(os.environ['NICHY_LOAD_SLOT'])
    write_json(job/('load-%d-ready.json'%slot),dict(pid=os.getpid(),uuid=expected,at=time.time(),
                                               tensor_bytes=3*size*size*4))
    duty, buffered, refreshed, cycles = 1.0,b'',time.monotonic(),0
    while not stopping[0]:
        readable,_,_ = select.select([sys.stdin.fileno()],[],[],0)
        if readable:
            data = os.read(sys.stdin.fileno(),4096)
            if not data:
                break  # Controller exited; do not leave an orphaned GPU load.
            buffered += data
            while b'\n' in buffered:
                line,buffered = buffered.split(b'\n',1)
                duty = float(line)
                if not math.isfinite(duty) or not .01<=duty<=1:
                    raise ValueError('invalid idle duty')
                refreshed = time.monotonic()
        if time.monotonic()-refreshed>5:
            break  # Also release CUDA if the controller stops responding.
        tick = time.monotonic()
        while not stopping[0] and time.monotonic()-tick < .1*duty:
            torch.mm(a,b,out=c)
            torch.cuda.synchronize()
            cycles += 1
        until = tick+.1
        while not stopping[0] and time.monotonic()<until:
            time.sleep(min(.01,max(0,until-time.monotonic())))
    write_json(job/('load-%d-result.json'%slot),dict(cycles=cycles,checked=True,uuid=expected))
