"""Bounded CUDA heartbeat. Scheduled by Nichy; never expands visible devices."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def gpu_stats():
    def query(fields, kind):
        p = subprocess.run(['nvidia-smi', '--query-'+kind+'='+fields,
                            '--format=csv,noheader,nounits'], capture_output=True,
                           text=True, check=True, timeout=2)
        return list(csv.reader(p.stdout.splitlines(), skipinitialspace=True))
    cards = {}
    for uid, util, total, used in query('uuid,utilization.gpu,memory.total,memory.used', 'gpu'):
        u, t, m = float(util), float(total), float(used)
        if not all(math.isfinite(x) for x in [u,t,m]) or t <= 0:
            raise ValueError('GPU telemetry unavailable')
        cards[uid] = dict(uuid=uid, utilization=u, free_percent=(t-m)*100/t, processes=[])
    for uid, pid in query('gpu_uuid,pid', 'compute-apps'):
        if uid in cards:
            cards[uid]['processes'].append(int(pid))
    return cards


def eligible(cards, visible, max_util=20, min_free=60):
    return [cards[uid] for uid in visible if uid in cards and
            cards[uid]['utilization'] < max_util and
            cards[uid]['free_percent'] > min_free and not cards[uid]['processes']]


def demand(card, min_free=60):
    # Container and NVML PIDs may use different namespaces. A pulse owns one
    # CUDA process; a second one is sufficient reason to yield, without killing it.
    return len(card['processes']) > 1 or card['free_percent'] < min_free or card['utilization'] >= 80


def probe():
    import torch
    out = []
    for i in range(torch.cuda.device_count()):
        uid = str(torch.cuda.get_device_properties(i).uuid)
        if not uid.startswith('GPU-'):
            uid = 'GPU-' + uid
        out.append(uid)
    return out


def pulse(seconds, duty, expected):
    import torch
    from mochi_core import write_json
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
    visible = probe()
    if visible != [expected]:
        raise ValueError('heartbeat device mapping changed')
    job = Path(os.environ['GPUQ_JOB_DIR'])
    card = gpu_stats().get(expected)
    # probe may initialize CUDA, but does not allocate our tensors yet.
    if card is None or card['utilization'] >= 20 or card['free_percent'] <= 60 or len(card['processes']) > 1:
        write_json(job/'pulse-result.json', dict(status='skipped-busy', cycles=0,
                   uuid=expected, seconds=0, checked=False))
        return
    torch.set_num_threads(1)
    torch.backends.cuda.matmul.allow_tf32 = False
    size = 2048
    a = torch.ones((size,size), device='cuda')
    b = torch.ones_like(a)
    torch.cuda.synchronize()
    write_json(job/'pulse-ready.json', dict(pid=os.getpid(), uuid=expected, at=time.time()))
    start, sampled, cycles = time.monotonic(), 0.0, 0
    status = 'complete'
    while time.monotonic()-start < seconds and not stopping[0]:
        if time.monotonic()-sampled >= .5:
            sampled = time.monotonic()
            try:
                card = gpu_stats()[expected]
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                status = 'telemetry-unavailable'; break
            if demand(card):
                status = 'yielded'; break
            active_duty = min(duty, .1 if card['utilization'] > 40 else .25)
        tick = time.monotonic()
        c = a @ b
        torch.cuda.synchronize()
        assert c[0,0].item() == size
        cycles += 1
        compute = time.monotonic()-tick
        # Keep individual waits short so cancellation can release the context.
        rest_until = time.monotonic() + max(.001, compute*(1/active_duty-1))
        while time.monotonic() < rest_until and not stopping[0]:
            time.sleep(min(.05, max(0, rest_until-time.monotonic())))
    if stopping[0]:
        status = 'yielded'
    write_json(job/'pulse-result.json', dict(status=status, cycles=cycles, uuid=expected,
               seconds=time.monotonic()-start, checked=True, tensor_bytes=3*size*size*4))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--duty', type=float, default=.25)
    parser.add_argument('--uuid')
    args=parser.parse_args()
    if args.probe:
        print(json.dumps(probe()))
    else:
        if not args.uuid or not 0 < args.seconds <= 120 or not .01 <= args.duty <= .25:
            parser.error('heartbeat requires an allocated UUID and bounded settings')
        pulse(args.seconds,args.duty,args.uuid)
