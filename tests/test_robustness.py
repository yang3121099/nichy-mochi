"""Linux fault-injection tests. Run on the target filesystem."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from test_base import QueueTest

SCRIPT = Path(__file__).resolve().parents[1] / 'mochi_core.py'

class RobustnessTest(QueueTest):
    # Inheritance deliberately reruns the original nine acceptance tests as well.
    def worker(self, *extra):
        f = (self.base / ('worker-%s.log' % len(self.workers))).open('w+')
        self.logs.append(f)
        p = subprocess.Popen([sys.executable,str(SCRIPT),'--root',str(self.root),'worker',
             '--poll','.02','--grace','.2','--idle-exit','30','--background-after','.05',*extra],stdout=f,stderr=f)
        self.workers.append(p)
        self.wait_for(lambda: (self.root/'worker.json').exists() and json.loads((self.root/'worker.json').read_text()).get('pid') == p.pid)
        return p
    def background(self, name, body, resume=False, max_preemptions=10):
        p=self.base/(name+'.sh');p.write_text(body)
        args=['submit',str(p),'--id',name,'--cwd',str(self.base),'--background', '--max-preemptions',str(max_preemptions)]
        if resume: args.append('--resume-safe')
        self.cli(*args)
    def assert_dead(self, pid):
        def gone():
            try: os.kill(pid,0); return False
            except ProcessLookupError: return True
        self.wait_for(gone)
    def test_background_preempts_before_foreground(self):
        self.background('bg','echo $$ > bg.pid\nexec sleep 30\n')
        self.worker();self.wait_for(lambda: (self.base/'bg.pid').exists())
        pid=int((self.base/'bg.pid').read_text())
        self.submit('fg', 'test ! -e /proc/%s\necho priority\n' % pid)
        self.wait_for(lambda: self.state('fg')=='SUCCEEDED')
        self.assertEqual(self.state('bg'),'PREEMPTED');self.assert_dead(pid)
    def test_background_resume_requires_explicit_flag(self):
        self.background('bg','if [ "$GPUQ_ATTEMPT" = 1 ]; then touch started; sleep 30; else echo resumed; fi\n',resume=True)
        self.worker();self.wait_for(lambda: (self.base/'started').exists())
        self.submit('fg')
        self.wait_for(lambda: self.state('bg')=='SUCCEEDED')
        d=json.loads((self.root/'jobs/bg/status.json').read_text())
        self.assertEqual(d['attempt'],2);self.assertEqual(d['preemptions'],1)
        self.assertEqual(self.state('fg'),'SUCCEEDED')
    def test_preemption_retry_cap(self):
        self.background('bg','touch started\nsleep 30\n',resume=True,max_preemptions=0)
        self.worker();self.wait_for(lambda: (self.base/'started').exists())
        self.submit('fg');self.wait_for(lambda: self.state('fg')=='SUCCEEDED')
        self.assertEqual(self.state('bg'),'PREEMPTED')
    def test_foreground_chosen_before_older_background(self):
        self.background('bg','test -e fg-done\necho bg\n')
        self.submit('fg','touch fg-done\n')
        self.worker();self.wait_for(lambda: self.state('bg')=='SUCCEEDED')
        self.assertEqual(self.state('fg'),'SUCCEEDED')
    def test_worker_sigkill_guard_cleans_and_no_replay(self):
        self.submit('victim','echo $$ > job.pid\nexec sleep 30\n')
        p=self.worker();self.wait_for(lambda: (self.base/'job.pid').exists())
        pid=int((self.base/'job.pid').read_text());p.kill();p.wait(3)
        self.assert_dead(pid)
        self.assertEqual(self.state('victim'),'RUNNING')
        self.cli('recover','--confirm-old-worker-stopped')
        self.assertEqual(self.state('victim'),'UNKNOWN')
        self.submit('next');self.worker();self.wait_for(lambda: self.state('next')=='SUCCEEDED')
        self.assertEqual(self.state('victim'),'UNKNOWN')
    def test_setsid_descendant_is_cleaned(self):
        self.submit('escape', "python3 -c 'import os,time; os.setsid(); open(\"escaped.pid\",\"w\").write(str(os.getpid())); time.sleep(30)' &\nwait\n")
        self.worker();self.wait_for(lambda: (self.base/'escaped.pid').exists() and (self.base/'escaped.pid').stat().st_size)
        pid=int((self.base/'escaped.pid').read_text())
        self.cli('cancel','escape');self.wait_for(lambda: self.state('escape')=='CANCELLED');self.assert_dead(pid)
    def test_detached_child_after_shell_exit(self):
        self.submit('escape', "python3 -c 'import os,time; os.setsid(); open(\"escaped.pid\",\"w\").write(str(os.getpid())); time.sleep(30)' &\nwhile [ ! -s escaped.pid ]; do sleep .01; done\nexit 0\n")
        self.worker();self.wait_for(lambda: self.state('escape')=='SUCCEEDED')
        self.assert_dead(int((self.base/'escaped.pid').read_text()))
    def test_term_ignoring_job_gets_killed(self):
        self.submit('ignore', "trap '' TERM\necho $$ > ignore.pid\nwhile :; do sleep 1; done\n")
        self.worker();self.wait_for(lambda: (self.base/'ignore.pid').exists())
        self.cli('cancel','ignore');self.wait_for(lambda: self.state('ignore')=='CANCELLED')
        self.assert_dead(int((self.base/'ignore.pid').read_text()))
    def test_bad_request_does_not_block_next(self):
        self.submit('bad');(self.root/'jobs/bad/request.json').write_text('{')
        self.submit('good');self.worker();self.wait_for(lambda: self.state('good')=='SUCCEEDED')
        self.assertEqual(self.state('bad'),'FAILED')
    def test_tampered_script_not_executed(self):
        self.submit('bad');(self.root/'jobs/bad/run.sh').write_text('touch UNEXPECTED\n')
        self.submit('good');self.worker();self.wait_for(lambda: self.state('good')=='SUCCEEDED')
        self.assertEqual(self.state('bad'),'FAILED');self.assertFalse((self.base/'UNEXPECTED').exists())
    def test_corrupt_status_fails_closed(self):
        self.submit('bad');(self.root/'jobs/bad/status.json').write_text('{')
        self.submit('good');p=self.worker();self.assertNotEqual(p.wait(3),0)
        self.assertEqual(self.state('good'),'PENDING')
        self.assertEqual(json.loads((self.root/'worker.json').read_text())['state'],'BLOCKED')
    def test_live_worker_recovery_refused(self):
        p=self.worker();self.assertNotEqual(self.cli('recover','--confirm-old-worker-stopped',ok=False).returncode,0)
        self.assertIsNone(p.poll())
    def test_worker_runtime_budget(self):
        self.submit('long','sleep 30\n');p=self.worker('--max-runtime','.4')
        self.assertEqual(p.wait(4),0);self.assertEqual(self.state('long'),'INTERRUPTED')
    def test_source_snapshot_executes_unchanged(self):
        src=self.base/'src';src.mkdir();(src/'a.py').write_text('print("snapshot")')
        sh=self.base/'code.sh';sh.write_text('cd "$GPUQ_CODE_DIR"\nexec python3 a.py\n')
        self.cli('submit',str(sh),'--source',str(src),'--cwd',str(self.base),'--id','code')
        (src/'a.py').write_text('raise RuntimeError("changed")')
        self.worker();self.wait_for(lambda: self.state('code')=='SUCCEEDED')
        self.assertIn('snapshot', self.cli('logs','code').stdout)

    def test_submitter_environment_is_not_inherited(self):
        self.submit('env', 'test "${GPUQ_SIDE:-}" != CPU\ntest "$GPUQ_JOB_ID" = env\necho separate-worker-environment\n')
        self.worker();self.wait_for(lambda:self.state('env')=='SUCCEEDED')

    def test_unrelated_process_survives_cancel(self):
        other=subprocess.Popen(['sleep','30'],start_new_session=True)
        try:
            self.submit('job','touch started\nsleep 30\n');self.worker()
            self.wait_for(lambda:(self.base/'started').exists());self.cli('cancel','job')
            self.wait_for(lambda:self.state('job')=='CANCELLED')
            self.assertIsNone(other.poll())
        finally:
            other.terminate();other.wait(3)

    def test_worker_sigstop_local_guard_deadline(self):
        self.submit('job','echo $$ > deadline.pid\nexec sleep 30\n',timeout='1')
        p=self.worker();self.wait_for(lambda:(self.base/'deadline.pid').exists())
        pid=int((self.base/'deadline.pid').read_text())
        os.kill(p.pid,signal.SIGSTOP)
        try:
            self.assert_dead(pid)
        finally:
            os.kill(p.pid,signal.SIGCONT)
        self.wait_for(lambda:self.state('job')=='TIMED_OUT')

    def test_idle_exit_releases_lock(self):
        p=self.worker('--idle-exit','.1')
        self.assertEqual(p.wait(4),0)
        self.assertFalse((self.root/'worker.lock').exists())

    def test_background_drain_preserves_resume(self):
        self.background('bg','touch started\nsleep 30\n',resume=True)
        p=self.worker();self.wait_for(lambda:(self.base/'started').exists())
        self.cli('stop');self.assertEqual(p.wait(4),0)
        self.assertEqual(self.state('bg'),'PENDING')

    def test_tampered_source_refused(self):
        src=self.base/'src';src.mkdir();(src/'a.py').write_text('print(1)')
        sh=self.base/'a.sh';sh.write_text('touch UNEXPECTED\n')
        self.cli('submit',str(sh),'--source',str(src),'--cwd',str(self.base),'--id','code')
        (self.root/'jobs/code/code/a.py').write_text('print(2)')
        self.worker();self.wait_for(lambda:self.state('code')=='FAILED')
        self.assertFalse((self.base/'UNEXPECTED').exists())

    def test_wait_completion_and_running_logs(self):
        self.submit('job','echo LIVE_LOG\ntouch started\nsleep .5\n')
        self.worker();self.wait_for(lambda:(self.base/'started').exists())
        self.assertIn('LIVE_LOG',self.cli('logs','job','--lines','1').stdout)
        self.assertEqual(self.cli('wait','job','--deadline','5').returncode,0)

if __name__ == '__main__':
    if sys.platform != 'linux':
        raise SystemExit('Linux /proc and prctl required; these tests cannot be validated on macOS.')
    # Avoid collecting the imported base class a second time.
    unittest.main(defaultTest='RobustnessTest', verbosity=2)
