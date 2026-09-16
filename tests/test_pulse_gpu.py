"""Actual CUDA heartbeat tests on an idle, allocated device; isolated queues."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock
from test_integration import IntegrationTest
from test_gpu import memory_used


class PulseTest(IntegrationTest):
    def start_pulse(self, seconds=30):
        self.worker(dict(enabled=True, interval=60, idle_for=.1, seconds=seconds, duty=.1))
        def ready():
            return list((self.root/'pulses').glob('*/pulse-ready.json'))
        self.wait_for(lambda:bool(ready()),timeout=40)
        path=ready()[0]
        self.pulse_job=path.parent
        self.pulse_pid=json.loads(path.read_text())['pid']
        return path.parent

    def wait_pulse_done(self, timeout=15):
        self.wait_for(lambda:json.loads((self.pulse_job/'status.json').read_text())['state'] in
                      {'SUCCEEDED','FAILED','PREEMPTED'}, timeout=timeout)

    def check_memory_returned(self, baseline):
        self.wait_for(lambda:all(v<=b+64 for v,b in zip(memory_used(),baseline)),timeout=12)

    def test_heartbeat_numerical_completion_and_memory(self):
        baseline=memory_used()
        job=self.start_pulse(seconds=2)
        self.wait_pulse_done()
        result=json.loads((job/'pulse-result.json').read_text())
        self.assertEqual(result['status'],'complete')
        self.assertTrue(result['checked']);self.assertGreater(result['cycles'],0)
        self.check_memory_returned(baseline)
        print(json.dumps(dict(test='heartbeat_complete',cycles=result['cycles'],
                              seconds=result['seconds'], tensor_bytes=result['tensor_bytes'],
                              memory_before_MiB=baseline,memory_after_MiB=memory_used())))

    def test_new_task_preempts_heartbeat_and_releases_gpu(self):
        baseline=memory_used()
        job=self.start_pulse()
        file=self.program('from pathlib import Path\nassert not Path("/proc/%d").exists()\n'
                          'import torch\nx=torch.ones(512,device="cuda")\nassert x.sum().item()==512\n'
                          'print("GPU belongs to my task")\n' % self.pulse_pid)
        result=self.nichy('run','--wait',file)
        self.assertIn('Mochi 完成了',result.stdout)
        state=json.loads((job/'status.json').read_text())
        self.assertEqual(state['state'],'PREEMPTED')
        foreground=self.newest()
        spec=json.loads((foreground/'request.json').read_text())
        status=json.loads((foreground/'status.json').read_text())
        self.assertLessEqual(state['finished_at'],status['started_at'])
        delay=status['started_at']-spec['submitted_at']
        self.assertLess(delay,8)
        self.check_memory_returned(baseline)
        print(json.dumps(dict(test='heartbeat_preemption',submit_to_start_seconds=delay,
                              memory_before_MiB=baseline,memory_after_MiB=memory_used())))

    def test_external_gpu_demand_yields_without_killing_other_job(self):
        job=self.start_pulse()
        ready=self.base/'external-ready'
        code=('import torch,time\nfrom pathlib import Path\n'
              'x=torch.ones(1024*1024,device="cuda")\ntorch.cuda.synchronize()\n'
              'Path(%r).write_text("ready")\ntime.sleep(30)\n' % str(ready))
        other=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.wait_for(lambda:ready.exists(),timeout=20)
            self.wait_pulse_done()
            result=json.loads((job/'pulse-result.json').read_text())
            self.assertEqual(result['status'],'yielded')
            self.assertLess(result['seconds'],30)
            self.assertIsNone(other.poll())
            print(json.dumps(dict(test='external_gpu_demand',pulse_seconds=result['seconds'],other_still_running=True)))
        finally:
            other.terminate();other.communicate(timeout=10)

    def test_config_changes_apply_only_after_restart(self):
        job=self.start_pulse(seconds=20)
        from mochi_core import write_json
        write_json(self.root/'config.json',dict(enabled=False))
        time.sleep(5.5)
        self.assertEqual(json.loads((job/'status.json').read_text())['state'],'RUNNING')
        self.workers[-1].terminate();self.workers[-1].wait(10)
        self.assertEqual(json.loads((job/'status.json').read_text())['state'],'INTERRUPTED')
        self.worker()
        self.assertEqual(json.loads((self.root/'keep_alive.json').read_text())['state'],'off')
        self.assertFalse(Path('/proc/%d'%self.pulse_pid).exists())
        self.nichy('run','--wait',self.program('print("normal task after disabling")'))

    def test_empty_visibility_never_expands_allocation(self):
        with mock.patch.dict(os.environ,{'CUDA_VISIBLE_DEVICES':''}):
            self.worker(dict(enabled=True,interval=60,idle_for=.1,seconds=2,duty=.1))
        state=self.root/'keep_alive.json'
        self.wait_for(lambda:state.exists() and json.loads(state.read_text())['state']=='no-visible-gpu',timeout=25)
        self.assertFalse((self.root/'pulses').exists())
        result=self.nichy('run','--wait',self.program('import torch\nassert torch.cuda.device_count()==0\nprint("CPU only")'))
        self.assertIn('Mochi 完成了',result.stdout)
