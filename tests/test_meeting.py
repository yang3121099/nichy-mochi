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
        self.wait_for(lambda:'RUN' in json.loads((self.root/'.signals.json').read_text()))
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
        self.wait_for(lambda:'第 1 次提交' in (self.root/'status.txt').read_text())
        (self.root/'STOP').touch()
        self.wait_for(lambda:self.state(newer.name)=='SUCCEEDED')
        self.assertEqual(self.state(old['job']),'CANCELLED')
        self.wait_for(lambda:'NEW' in (self.root/'log').read_text())

    def test_invalid_shell_rejected_then_corrected(self):
        self.worker()
        self.send_command('if then\n')
        self.wait_for(lambda:'RUN' in json.loads((self.root/'.signals.json').read_text()))
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

    def test_upload_command_without_run_marker(self):
        self.worker()
        (self.root/'command.sh').write_text('echo AUTO_UPLOAD\nsleep 2\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        self.wait_for(lambda:'AUTO_UPLOAD' in (self.root/'log').read_text())
        self.assertFalse((self.root/'RUN').exists())
        self.assertEqual(json.loads((self.root/'worker.json').read_text())['state'],'IDLE')

    def test_partial_upload_and_rename(self):
        self.worker()
        temporary=self.root/'command.sh.upload'
        temporary.write_text('echo INCOMPLETE\n')
        time.sleep(2.5)
        self.assertEqual(list((self.root/'jobs').iterdir()),[])
        temporary.write_text('echo COMPLETE_UPLOAD\n')
        temporary.replace(self.root/'command.sh')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        self.wait_for(lambda:'COMPLETE_UPLOAD' in (self.root/'log').read_text())
        self.assertNotIn('INCOMPLETE',(self.root/'log').read_text())

    def test_auto_restart_and_offline_upload(self):
        worker=self.worker()
        (self.root/'command.sh').write_text('echo FIRST_AUTO\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        worker.terminate();worker.wait(10)
        worker=self.worker();time.sleep(3)
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
        worker.terminate();worker.wait(10)
        (self.root/'command.sh').write_text('echo OFFLINE_UPLOAD\n')
        self.worker()
        self.wait_for(lambda:'OFFLINE_UPLOAD' in (self.root/'log').read_text())
        self.assertEqual(len(list((self.root/'jobs').iterdir())),2)

    def test_auto_publication_receipt_crash_gap(self):
        worker=self.worker()
        baseline=json.loads((self.root/'.signals.json').read_text())
        (self.root/'command.sh').write_text('echo EXACTLY_ONCE\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        worker.terminate();worker.wait(10)
        (self.root/'.signals.json').write_text(json.dumps(baseline))
        self.worker();time.sleep(3)
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
        self.assertEqual((self.root/'log').read_text().count('EXACTLY_ONCE'),1)

    def test_auto_same_command_reupload_is_new_job(self):
        self.worker()
        command=self.root/'command.sh'
        command.write_text('echo SAME_COMMAND\n')
        self.wait_for(lambda:(self.root/'receipt.json').exists())
        job=json.loads((self.root/'receipt.json').read_text())['job']
        self.wait_for(lambda:self.state(job)=='SUCCEEDED')
        command.write_text('echo SAME_COMMAND\n')
        self.wait_for(lambda:len(list((self.root/'jobs').iterdir()))==2)
        self.wait_for(lambda:(self.root/'log').read_text().count('SAME_COMMAND')==2)

    def test_auto_invalid_shell_then_corrected(self):
        self.worker()
        (self.root/'command.sh').write_text('if then\n')
        self.wait_for(lambda:json.loads((self.root/'.signals.json').read_text())['command.sh']['state']=='REJECTED')
        self.assertEqual(list((self.root/'jobs').iterdir()),[])
        self.wait_for(lambda:'未接收' in (self.root/'status.txt').read_text())
        (self.root/'command.sh').write_text('echo REPAIRED_UPLOAD\n')
        self.wait_for(lambda:'REPAIRED_UPLOAD' in (self.root/'log').read_text())

    def test_cli_command_does_not_duplicate_auto_upload(self):
        self.worker()
        (self.root/'command.sh').write_text('echo ONE_CLI_COMMAND\n')
        env=dict(self.client_env,NICHY_HOME=str(self.root),NICHY_CLIENT_PYTHON=self.client_python)
        result=subprocess.run(['bash',str(APP.parent/'submit.sh')],env=env,cwd=self.base,
                         capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        time.sleep(3)
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)

    def test_heartbeat_has_its_own_quiet_log(self):
        self.worker()
        log=self.root/'keep_alive.log'
        self.assertIn('心跳 · 已关闭',log.read_text())
        before=log.read_text();time.sleep(1)
        self.assertEqual(log.read_text(),before)

    def test_names_full_timestamps_and_searchable_numbers(self):
        import csv
        self.worker()
        self.send_command('echo FRIENDLY_OUTPUT\n')
        self.wait_for(lambda:'Mochi 完成了' in (self.root/'log').read_text())
        text=(self.root/'log').read_text()
        self.assertIn('Nichy 提交了：command.sh · ',text)
        self.assertIn('第 1 次提交',text)
        self.assertIn('Mochi 输出：command.sh · ',text)
        self.assertLess(text.index('Nichy 提交了'),text.index('Mochi 收到了'))
        events=[line for line in text.splitlines() if line.startswith('[')]
        for line in events:
            self.assertRegex(line,r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ')
        receipt=json.loads((self.root/'receipt.json').read_text())
        self.assertEqual(receipt['submission'],1)
        with (self.root/'submissions.tsv').open() as stream:
            records=list(csv.DictReader(stream,delimiter='\t'))
        self.assertEqual(records[0]['提交'],receipt['submission_label'])
        self.assertTrue(records[0]['提交'].endswith('第 1 次提交'))
        self.assertEqual(records[0]['完整版本'],receipt['revision'])
        self.assertEqual(Path(records[0]['任务目录']).name,receipt['job'])
        self.assertEqual(records[0]['备份'],receipt['backup'])
        self.assertEqual((self.root/receipt['backup']).read_text(),'echo FRIENDLY_OUTPUT\n')
        self.assertEqual(receipt['hour_submission'],1)
        self.assertRegex(receipt['submission_hour'],r'^\d{4}-\d{2}-\d{2}_\d{2}$')
        self.assertNotIn('+08:00',text)

    def test_numbers_survive_restart_and_archived_jobs(self):
        import shutil
        worker=self.worker()
        self.send_command('echo NUMBER_ONE\n')
        self.wait_for(lambda:'Mochi 完成了' in (self.root/'log').read_text())
        first=json.loads((self.root/'receipt.json').read_text())
        worker.terminate();worker.wait(10)
        worker=self.worker()
        self.send_command('echo NUMBER_ONE\n')
        self.wait_for(lambda:json.loads((self.root/'receipt.json').read_text())['submission']==2)
        second=json.loads((self.root/'receipt.json').read_text())
        self.wait_for(lambda:self.state(second['job'])=='SUCCEEDED')
        self.assertEqual(first['revision'],second['revision'])
        worker.terminate();worker.wait(10)
        shutil.rmtree(self.root/'jobs'/first['job'])
        shutil.rmtree(self.root/'jobs'/second['job'])
        self.assertEqual((self.root/first['backup']).read_text(),'echo NUMBER_ONE\n')
        self.assertEqual((self.root/second['backup']).read_text(),'echo NUMBER_ONE\n')
        self.worker()
        self.send_command('echo NUMBER_THREE\n')
        self.wait_for(lambda:json.loads((self.root/'receipt.json').read_text())['submission']==3)
        self.assertIn('第 3 次提交',(self.root/'submissions.tsv').read_text())

    def test_concurrent_submissions_have_unique_numbers(self):
        from concurrent.futures import ThreadPoolExecutor
        self.worker()
        file=self.program('print("parallel submission")')
        with ThreadPoolExecutor(max_workers=4) as clients:
            list(clients.map(lambda _:self.nichy('run',file),range(4)))
        self.wait_for(lambda:len(json.loads((self.root/'.submissions.json').read_text()))==4)
        records=json.loads((self.root/'.submissions.json').read_text())
        self.assertEqual(sorted(row['number'] for row in records.values()),[1,2,3,4])
