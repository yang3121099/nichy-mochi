"""Bounded CUDA tests: numerical check, preemption/resume, memory return.
Run only on an allocated idle GPU. Uses < 128 MiB tensors, < 90 sec/job.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_robustness import RobustnessTest

DEMO=Path(__file__).resolve().parents[1]/'examples/cuda_check.py'

def memory_used():
    r=subprocess.run(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],capture_output=True,text=True,check=True,timeout=5)
    return [int(v) for v in r.stdout.splitlines()]

class CudaTest(RobustnessTest):
    # Only collect the three CUDA-specific tests below.
    def gpu_submit(self,name,chunks=12,background=False,resume=False):
        import shlex
        body='exec '+shlex.quote(sys.executable)+' -u '+shlex.quote(str(DEMO))+' --chunks '+str(chunks)+' --pause .1\n'
        if background: self.background(name,body,resume=resume)
        else: self.submit(name,body,timeout='90')
    def test_cuda_numerical(self):
        self.gpu_submit('cuda',3);self.worker('--grace','3')
        self.wait_for(lambda:self.state('cuda') in {'SUCCEEDED','FAILED'},timeout=40)
        self.assertEqual(self.state('cuda'),'SUCCEEDED',self.cli('logs','cuda').stdout)
        result=json.loads((self.root/'jobs/cuda/demo-result.json').read_text())
        self.assertTrue(result['complete'])
    def test_cuda_background_preemption(self):
        baseline=memory_used()
        self.gpu_submit('bg',50,True,True);self.worker('--grace','3','--background-after','.1')
        ready=self.root/'jobs/bg/gpu-ready.json'
        self.wait_for(lambda:ready.exists(),timeout=40)
        pid=json.loads(ready.read_text())['pid']
        t=time.monotonic()
        self.submit('fg', 'test ! -e /proc/%s\n%s -u %s --chunks 2 --pause 0\n' % (pid,sys.executable,DEMO),timeout='90')
        self.wait_for(lambda:self.state('fg') in {'SUCCEEDED','FAILED'},timeout=40)
        latency=time.monotonic()-t
        self.assertEqual(self.state('fg'),'SUCCEEDED',self.cli('logs','fg').stdout)
        self.wait_for(lambda:self.state('bg') in {'SUCCEEDED','FAILED'},timeout=40)
        self.assertEqual(self.state('bg'),'SUCCEEDED',self.cli('logs','bg').stdout)
        bg=json.loads((self.root/'jobs/bg/status.json').read_text())
        self.assertGreaterEqual(bg['preemptions'],1)
        cp=json.loads((self.root/'jobs/bg/checkpoint.json').read_text())
        self.assertEqual([x['chunk'] for x in cp['results']],list(range(50)))
        end=time.monotonic()+10
        after=memory_used()
        while any(v>b+64 for v,b in zip(after,baseline)) and time.monotonic()<end:
            time.sleep(.2);after=memory_used()
        self.assertEqual(len(after),len(baseline))
        self.assertTrue(all(v<=b+64 for v,b in zip(after,baseline)),(baseline,after))
        print(json.dumps({'test':'cuda_background_preemption','foreground_submit_to_finish_seconds':latency,'memory_before_MiB':baseline,'memory_after_MiB':after,'attempts':bg['attempt']}))
    def test_cuda_worker_sigkill(self):
        self.gpu_submit('victim',100);p=self.worker('--grace','3')
        ready=self.root/'jobs/victim/gpu-ready.json'
        self.wait_for(lambda:ready.exists(),timeout=40)
        pid=json.loads(ready.read_text())['pid'];p.kill();p.wait(3)
        self.assert_dead(pid)
        self.assertEqual(self.state('victim'),'RUNNING')
        self.cli('recover','--confirm-old-worker-stopped')
        self.assertEqual(self.state('victim'),'UNKNOWN')

if __name__=='__main__':
    names=['test_cuda_numerical','test_cuda_background_preemption','test_cuda_worker_sigkill']
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CudaTest(n) for n in names))
    raise SystemExit(0 if result.wasSuccessful() else 1)
