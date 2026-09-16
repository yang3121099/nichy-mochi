#!/usr/bin/env python3
"""Finite CUDA checkpoint/preemption fixture, not an idle occupancy service.
Each chunk validates a matrix multiplication and commits one result atomically.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mochi_core import write_json

p=argparse.ArgumentParser()
p.add_argument('--chunks', type=int, default=12)
p.add_argument('--size', type=int, default=512)
p.add_argument('--pause', type=float, default=.1)
p.add_argument('--deadline', type=float, default=30)
a=p.parse_args()
if not (1 <= a.chunks <= 10000 and 32 <= a.size <= 2048 and 0 <= a.pause <= 1 and 0 < a.deadline <= 300):
    p.error('fixture limits exceeded')
import torch
if not torch.cuda.is_available():
    raise SystemExit('CUDA unavailable')
torch.set_num_threads(1)
stop=[False]
signal.signal(signal.SIGTERM,lambda *_: stop.__setitem__(0,True))
job=Path(os.environ['GPUQ_JOB_DIR'])
state_path=job/'checkpoint.json'
state=json.loads(state_path.read_text()) if state_path.exists() else {'next':0,'results':[]}
assert state['next'] == len(state['results'])
started=time.monotonic()
x=torch.eye(a.size, device='cuda')
w=torch.arange(a.size,device='cuda',dtype=torch.float32).repeat(a.size,1)
torch.cuda.synchronize()
write_json(job/'gpu-ready.json',{'pid':os.getpid(),'time':time.time(),'allocated':torch.cuda.memory_allocated(), 'device':torch.cuda.get_device_name()})
for chunk in range(state['next'], a.chunks):
    if stop[0]: break
    if time.monotonic()-started >= a.deadline: raise SystemExit('fixture deadline reached')
    y=x@w
    assert torch.equal(y,w)
    state['results'].append({'chunk':chunk,'checksum':float(y.sum().item())})
    state['next']=chunk+1
    write_json(state_path,state)
    print(json.dumps({'chunk':chunk,'cuda':True,'allocated':torch.cuda.memory_allocated()}),flush=True)
    time.sleep(a.pause)
write_json(job/'demo-result.json',{'complete':state['next']==a.chunks,'chunks':state['next'], 'total':a.chunks, 'stopped':stop[0]})
