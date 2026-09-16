"""Linux file-only submission and automatically mirrored conversation tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from test_integration import IntegrationTest, APP


class MeetingTest(IntegrationTest):
    def test_log_available_before_first_submission(self):
        self.worker()
        self.assertIn('Mochi 已就位',(self.root/'log').read_text())
        self.assertEqual(list((self.root/'jobs').iterdir()),[])

    def send_command(self, text):
        command=self.root/'command.sh'
        command.write_text(text)
        (self.root/'RUN').touch()
        return command

    def test_file_only_submit_receipt_and_live_log(self):
        self.worker()
        self.send_command('echo FIRST_OUTPUT\nsleep 2\necho LAST_OUTPUT\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:'FIRST_OUTPUT' in (self.root/'log').read_text())
        self.assertEqual(self.state(job),'RUNNING')
        self.assertIn('Mochi 收到了', (self.root/'log').read_text())
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        self.wait_for(lambda:'LAST_OUTPUT' in (self.root/'log').read_text())
        self.wait_for(lambda:'Mochi 完成了' in (self.root/'status.txt').read_text())

    def test_marker_not_replayed_after_restart(self):
        worker=self.worker()
        self.send_command('echo ONE_TIME\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        worker.terminate();worker.wait(10)
        self.worker()
        time.sleep(1)
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
        self.assertEqual((self.root/'log').read_text().count('ONE_TIME'),1)
        (self.root/'RUN').touch()
        self.wait_for(lambda:len(list((self.root/'jobs').iterdir()))==2)

    def test_crash_between_publish_and_receipt_does_not_resubmit(self):
        worker=self.worker()
        self.send_command('echo PUBLISHED_ONCE\n')
        self.wait_for(lambda:(self.root/'.signals.json').exists())
        job=json.loads((self.root/'.signals.json').read_text())['RUN']['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        worker.terminate();worker.wait(10)
        (self.root/'.signals.json').unlink()
        (self.root/'command.sh').write_text('echo CHANGED_AFTER_PUBLISH\n')
        self.worker();time.sleep(1)
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
        self.assertNotIn('CHANGED_AFTER_PUBLISH',(self.root/'log').read_text())

    def test_new_command_and_stop_marker_during_running_job(self):
        self.worker()
        self.send_command('echo OLD\nsleep 30\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        old=json.loads((self.root/'receipt.json').read_text())
        self.wait_for(lambda:self.state(old['job'])=='RUNNING')
        self.send_command('echo NEW\n')
        self.wait_for(lambda:len(list((self.root/'jobs').iterdir()))==2)
        newer=self.newest()
        self.wait_for(lambda:(newer/'received.json').exists())
        self.assertEqual(self.state(newer.name),'PENDING')
        self.assertIn(old['revision'][:8],(self.root/'status.txt').read_text())
        (self.root/'STOP').touch()
        self.wait_for(lambda:self.state(newer.name)=='SUCCEEDED')
        self.assertEqual(self.state(old['job']),'CANCELLED')
        self.wait_for(lambda:'NEW' in (self.root/'log').read_text())

    def test_invalid_shell_rejected_then_corrected(self):
        self.worker()
        self.send_command('if then\n')
        self.wait_for(lambda:(self.root/'.signals.json').exists())
        self.assertEqual(json.loads((self.root/'.signals.json').read_text())['RUN']['state'],'REJECTED')
        self.assertEqual(list((self.root/'jobs').iterdir()),[])
        self.send_command('echo FIXED\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')

    def test_submit_sh_streams_without_installed_cli(self):
        self.worker()
        file=self.program('print("STREAMED_PROGRAM_OUTPUT")\n')
        env=dict(self.client_env,NICHY_HOME=str(self.root),NICHY_CLIENT_PYTHON=self.client_python)
        p=subprocess.run(['bash',str(APP.parent/'submit.sh'),file],env=env,cwd=self.base,
                         capture_output=True,text=True,timeout=12)
        self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        self.assertIn('Mochi 收到了',p.stdout)
        self.assertIn('STREAMED_PROGRAM_OUTPUT',p.stdout)
        self.assertIn('Mochi 完成了',p.stdout)

    def test_command_sh_submission_without_arguments(self):
        self.worker()
        (self.root/'command.sh').write_text('python -c \'import sys; print(sys.executable)\'\n')
        env=dict(self.client_env,NICHY_HOME=str(self.root),NICHY_CLIENT_PYTHON=self.client_python)
        p=subprocess.run(['bash',str(APP.parent/'submit.sh')],env=env,cwd=self.base,
                         capture_output=True,text=True,timeout=12)
        self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        self.assertIn(sys.executable,p.stdout)
        self.assertIn('Mochi 完成了',p.stdout)

    def test_existing_log_and_heartbeat_file_preserved(self):
        self.root.mkdir()
        (self.root/'log').write_text('EXISTING_LOG\n')
        helper=self.root/'keep_alive.py';helper.write_text('# existing user helper\n')
        self.worker()
        self.send_command('echo APPENDED\n')
        self.wait_for(lambda:'APPENDED' in (self.root/'log').read_text())
        self.assertTrue((self.root/'log').read_text().startswith('EXISTING_LOG\n'))
        self.assertEqual(helper.read_text(),'# existing user helper\n')
