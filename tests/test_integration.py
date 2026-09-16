"""Linux end-to-end dialogue tests. Submitters have no CUDA visibility."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from test_base import QueueTest

APP = Path(__file__).resolve().parents[1] / 'nichy.py'


class IntegrationTest(QueueTest):
    def test_start_sh_initializes_once_and_accepts_chosen_path(self):
        self.root=self.base/'shared folder'
        def launch():
            log=(self.base/('start-%d.log'%len(self.logs))).open('w+')
            self.logs.append(log)
            process=subprocess.Popen(['bash',str(APP.parent/'start.sh'),str(self.root)],
                       env=dict(os.environ,NICHY_PYTHON=sys.executable,CUDA_VISIBLE_DEVICES=''),stdout=log,stderr=log)
            self.workers.append(process)
            self.wait_for(lambda:(self.root/'worker.json').exists() and
                json.loads((self.root/'worker.json').read_text()).get('pid')==process.pid,timeout=10)
            return process
        first=launch()
        config=self.root/'config.json'
        self.assertEqual(json.loads(config.read_text())['interval'],1800)
        first.terminate();first.wait(10)
        custom='{"enabled":false,"interval":75}'
        config.write_text(custom)
        launch()
        self.assertEqual(config.read_text(),custom)
        self.assertEqual(json.loads((self.root/'keep_alive.json').read_text())['config']['interval'],75)
        self.nichy('run','--wait',self.program('print("chosen folder")'))

    def test_business_start_sh_runs_as_normal_task(self):
        self.worker()
        file=Path(self.program('print("unused")')).with_name('start.sh')
        file.write_text('printf "business started: %s\\n" "$1"\n')
        self.nichy('run','--wait',str(file),'hello world')
        self.assertIn('business started: hello world',self.nichy('log').stdout)

    def worker(self, config=None):
        self.root.mkdir(exist_ok=True)
        if config is not None or not (self.root/'config.json').exists():
            (self.root/'config.json').write_text(json.dumps(config if config is not None else dict(enabled=False)))
        log = (self.base/'nichy-worker.log').open('w+')
        self.logs.append(log)
        process = subprocess.Popen([sys.executable, str(APP), '--home', str(self.root), 'serve'],
                                   stdout=log, stderr=log)
        self.workers.append(process)
        self.wait_for(lambda: (self.root/'worker.json').exists() and
                      json.loads((self.root/'worker.json').read_text()).get('pid')==process.pid, timeout=10)
        return process

    def nichy(self, *args, ok=True):
        result = subprocess.run([self.client_python, str(APP), '--home', str(self.root), *args],
                   env=self.client_env, cwd=self.base, capture_output=True, text=True, timeout=15)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr+'\n'+result.stdout)
        with (self.base/'dialogue.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(args=args, code=result.returncode, stdout=result.stdout, stderr=result.stderr))+'\n')
        return result

    def program(self, body):
        project=self.base/'project'; project.mkdir(exist_ok=True)
        file=project/'hello.py';file.write_text(body)
        return str(file)

    def newest(self):
        return max((self.root/'jobs').iterdir(),key=lambda j:json.loads((j/'request.json').read_text())['submitted_at'])

    def test_run_receipt_and_completion_dialogue(self):
        self.worker()
        result=self.nichy('run','--wait',self.program('print("你好 Mochi")\n'))
        self.assertIn('Mochi 收到了：hello.py · ',result.stdout)
        self.assertIn('Mochi 完成了 ✓',result.stdout)
        self.assertIn('你好 Mochi',self.nichy('log').stdout)
        job=self.newest()
        spec=json.loads((job/'request.json').read_text())
        receipt=json.loads((job/'received.json').read_text())
        self.assertEqual(receipt['revision'],spec['revision'])

    def test_new_receipt_while_old_revision_runs(self):
        self.worker()
        file=self.program('import time\nprint("old content",flush=True)\ntime.sleep(30)\n')
        self.nichy('run',file)
        old=self.newest()
        self.wait_for(lambda: self.state(old.name)=='RUNNING')
        old_revision=json.loads((old/'request.json').read_text())['revision'][:8]
        Path(file).write_text('print("new content")\n')
        result=self.nichy('run',file)
        new=self.newest()
        self.assertIn('Mochi 收到了',result.stdout)
        new_revision=json.loads((new/'request.json').read_text())['revision'][:8]
        self.assertNotEqual(old_revision,new_revision)
        self.assertEqual(self.state(new.name),'PENDING')
        status=self.nichy().stdout
        self.assertIn('第 1 次提交',status);self.assertIn('第 2 次提交',status)
        self.assertIn('排队中',status)
        self.nichy('stop')
        self.wait_for(lambda:self.state(new.name)=='SUCCEEDED')
        self.assertEqual(self.state(old.name),'CANCELLED')
        self.assertIn('new content',self.nichy('log').stdout)
        self.assertIn('old content',(old/'run.log').read_text())

    def test_configuration_read_once_and_bad_config_does_not_block_tasks(self):
        worker=self.worker()
        config=self.root/'config.json'
        state=self.root/'keep_alive.json'
        self.wait_for(lambda:state.exists())
        config.write_text('{broken')
        time.sleep(5.5)
        self.assertEqual(json.loads(state.read_text())['state'],'off')
        worker.terminate();worker.wait(10)
        self.worker()
        self.wait_for(lambda:json.loads(state.read_text())['state']=='config-error',timeout=8)
        result=self.nichy('run','--wait',self.program('print("still works")'))
        self.assertIn('Mochi 完成了',result.stdout)

    def test_tampered_snapshot_never_acknowledged(self):
        self.nichy('run',self.program('print("original")'))
        job=self.newest()
        (job/'code/hello.py').write_text('raise RuntimeError("tampered")')
        self.worker()
        self.wait_for(lambda:self.state(job.name)=='FAILED')
        self.assertEqual(json.loads((job/'received.json').read_text())['state'],'REJECTED')
        self.assertFalse((job/'run.log').exists())

    def test_task_failure_and_timeout_are_distinct(self):
        self.worker()
        result=self.nichy('run','--wait',self.program('raise RuntimeError("test failure")'),ok=False)
        self.assertEqual(result.returncode,1)
        self.assertIn('Mochi 遇到问题了',result.stdout)
        self.assertIn('test failure',self.nichy('log').stdout)
        result=self.nichy('run','--wait','--timeout','.2',self.program('import time\ntime.sleep(30)'),ok=False)
        self.assertEqual(result.returncode,1)
        self.assertIn('时间上限',result.stdout)

    def test_stopped_worker_preserves_new_submission(self):
        worker=self.worker()
        worker.terminate();worker.wait(10)
        result=self.nichy('run',self.program('print("later")'))
        self.assertNotIn('Mochi 收到了',result.stdout)
        job=self.newest()
        self.assertEqual(self.state(job.name),'PENDING')
        self.worker()
        self.wait_for(lambda:self.state(job.name)=='SUCCEEDED')
