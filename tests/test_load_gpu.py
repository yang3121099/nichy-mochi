"""Actual continuous CUDA load, multi-device cleanup and priority acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from keep_alive import gpu_stats
from test_gpu import memory_used
from test_integration import IntegrationTest


class ContinuousGpuTest(IntegrationTest):
    def start_continuous(self):
        self.baseline=memory_used()
        self.worker(dict(enabled=True,mode='continuous',target_utilization=90,seconds=180))
        return self.wait_ready()

    def wait_ready(self, excluding=None):
        def ready():
            return [p for p in (self.root/'pulses').glob('*/pulse-ready.json') if p.parent!=excluding]
        self.wait_for(lambda:bool(ready()),timeout=75)
        path=max(ready(),key=lambda p:p.stat().st_mtime)
        return path.parent,json.loads(path.read_text())

    def stop_and_check_memory(self):
        self.workers[-1].terminate();self.workers[-1].wait(15)
        self.wait_for(lambda:all(v<=b+64 for v,b in zip(memory_used(),self.baseline)),timeout=15)

    def test_idle_average_above_75_on_every_visible_gpu(self):
        job,ready=self.start_continuous()
        values={uid:[] for uid in ready['uuids']}
        time.sleep(3)
        for _ in range(30):
            observed=gpu_stats()
            for uid in values:values[uid].append(observed[uid]['utilization'])
            time.sleep(1)
        averages={uid:sum(samples)/len(samples) for uid,samples in values.items()}
        print(json.dumps(dict(test='continuous_idle_utilization',averages=averages,samples=values)))
        self.assertTrue(all(value>75 for value in averages.values()),averages)
        self.assertEqual(json.loads((job/'status.json').read_text())['state'],'RUNNING')
        self.stop_and_check_memory()

    def test_real_task_starts_after_all_idle_children_die_then_idle_resumes(self):
        job,ready=self.start_continuous()
        file=self.program('from pathlib import Path\nimport time,torch\n'
            'for pid in '+repr(ready['pids']+[ready['pid']])+':\n'
            '    assert not Path("/proc/%d"%pid).exists()\n'
            'for i in range(torch.cuda.device_count()):\n'
            '    x=torch.ones(1024,device="cuda:%d"%i)\n'
            '    assert x.sum().item()==1024\n'
            'time.sleep(2)\nprint("real task owns all GPUs")\n')
        self.nichy('run',file)
        task=self.newest()
        self.wait_for(lambda:self.state(task.name) in {'SUCCEEDED','FAILED'},timeout=60)
        self.assertEqual(self.state(task.name),'SUCCEEDED',(task/'run.log').read_text())
        before=json.loads((job/'status.json').read_text())
        after=json.loads((task/'status.json').read_text())
        self.assertEqual(before['state'],'PREEMPTED')
        self.assertTrue(before['cleanup_confirmed'])
        self.assertTrue(after['cleanup_confirmed'])
        self.assertLessEqual(before['finished_at'],after['started_at'])
        new_job,new_ready=self.wait_ready(excluding=job)
        self.assertNotEqual(new_ready['pid'],ready['pid'])
        print(json.dumps(dict(test='continuous_preempt_and_resume',idle_children=len(ready['pids']),
            submit_to_start_seconds=after['started_at']-json.loads((task/'request.json').read_text())['submitted_at'])))
        self.stop_and_check_memory()

    def test_torchrun_cpu_loading_and_saving_never_trigger_idle_load(self):
        old,ready=self.start_continuous()
        program=Path(self.program('import os,time,torch\nfrom pathlib import Path\n'
            'job=Path(os.environ["GPUQ_JOB_DIR"])\n'
            'def phase(name):\n'
            '    print(name,flush=True)\n'
            '    (job/("phase-"+name)).touch()\n'
            '    until=time.monotonic()+30\n'
            '    while not (job/("continue-"+name)).exists():\n'
            '        assert time.monotonic()<until,"test phase timed out"\n'
            '        time.sleep(.1)\n'
            'phase("loading")\n'
            'x=torch.ones((1024,1024),device="cuda")\n'
            'y=x@x\ntorch.cuda.synchronize()\n'
            'assert y[0,0].item()==1024\n'
            'print("Epoch 1/1 complete",flush=True)\n'
            'del x,y\ntorch.cuda.empty_cache()\n'
            'phase("saving")\nprint("RESULT SAVED",flush=True)\n'))
        shell=program.with_name('train.sh')
        shell.write_text('exec "$NICHY_PYTHON" -m torch.distributed.run --standalone --nnodes=1 --nproc-per-node=1 "$GPUQ_CODE_DIR/hello.py"\n')
        self.nichy('run',str(shell));task=self.newest()
        for phase in ['loading','saving']:
            self.wait_for(lambda:(task/('phase-'+phase)).exists(),timeout=35)
            time.sleep(2)
            for _ in range(2):
                observed=gpu_stats()
                self.assertTrue(all(observed[uid]['utilization']==0 for uid in ready['uuids']),observed)
                self.assertEqual(self.state(task.name),'RUNNING')
                self.assertFalse(any(self.state_in(p)=='RUNNING' for p in (self.root/'pulses').iterdir()))
                progress=json.loads((self.root/'progress.json').read_text())
                self.assertEqual(progress['job'],task.name)
                self.assertEqual(progress['phase'],'running')
                self.assertTrue(progress['supervisor_alive'])
                time.sleep(.5)
            (task/('continue-'+phase)).touch()
        self.wait_for(lambda:self.state(task.name) in {'SUCCEEDED','FAILED'},timeout=15)
        self.assertEqual(self.state(task.name),'SUCCEEDED',(task/'run.log').read_text())
        result=json.loads((task/'status.json').read_text())
        self.assertTrue(result['cleanup_confirmed'])
        self.assertTrue(json.loads((task/result['guard_record']).read_text())['cleaned'])
        self.assertIn('RESULT SAVED',(task/'run.log').read_text())
        self.wait_ready(excluding=old)
        self.stop_and_check_memory()

    def test_failed_real_task_restores_load_only_after_cleanup(self):
        job,ready=self.start_continuous()
        self.nichy('run',self.program('raise RuntimeError("deliberate task failure")'))
        task=self.newest()
        self.wait_for(lambda:self.state(task.name)=='FAILED',timeout=20)
        result=json.loads((task/'status.json').read_text())
        self.assertTrue(result['cleanup_confirmed'])
        resumed,_=self.wait_ready(excluding=job)
        self.assertGreaterEqual(json.loads((resumed/'status.json').read_text())['started_at'],result['finished_at'])
        self.stop_and_check_memory()

    def test_external_demand_yields_all_cards_and_resumes_after_exit(self):
        job,ready=self.start_continuous()
        marker=self.base/'external-ready'
        other=subprocess.Popen([sys.executable,'-c','import torch,time\nfrom pathlib import Path\n'
            'x=torch.ones(1024,device="cuda")\ntorch.cuda.synchronize()\n'
            'Path('+repr(str(marker))+').write_text("ready")\ntime.sleep(45)'])
        try:
            self.wait_for(marker.exists,timeout=25)
            self.wait_for(lambda:self.state_in(job) in {'SUCCEEDED','FAILED'},timeout=12)
            self.assertEqual(json.loads((job/'pulse-result.json').read_text())['status'],'external-demand')
            self.assertIsNone(other.poll())
            for pid in ready['pids']:self.assertFalse(Path('/proc/%d'%pid).exists())
            time.sleep(2)
            self.assertFalse(any(self.state_in(p)=='RUNNING' for p in (self.root/'pulses').iterdir()))
        finally:
            other.terminate();other.wait(10)
        self.wait_ready(excluding=job)
        self.stop_and_check_memory()

    @staticmethod
    def state_in(job):
        return json.loads((job/'status.json').read_text())['state']

    def test_worker_sigkill_cleans_all_idle_children(self):
        job,ready=self.start_continuous()
        self.workers[-1].kill();self.workers[-1].wait(10)
        self.wait_for(lambda:all(not Path('/proc/%d'%pid).exists() for pid in ready['pids']),timeout=15)
        self.wait_for(lambda:all(v<=b+64 for v,b in zip(memory_used(),self.baseline)),timeout=15)
