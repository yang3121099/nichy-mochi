"""Portable behavior tests for the command and GPU selection rules."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nichy
from keep_alive import eligible, demand, gpu_stats
from mochi_worker import NichyWorker


class DialogueTest(unittest.TestCase):
    def test_missing_default_falls_back_without_creating_users_path(self):
        original=Path.is_dir
        def is_dir(path):
            return False if str(path)=='/users/nichy/code/start' else original(path)
        with tempfile.TemporaryDirectory() as directory:
            module=Path(directory)/'nichy.py'
            errors=io.StringIO()
            with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(nichy,'__file__',str(module)),mock.patch.object(Path,'is_dir',is_dir),contextlib.redirect_stderr(errors):
                chosen=nichy.queue_root()
            self.assertEqual(chosen,(Path(directory)/'.nichy').resolve())
            self.assertIn('暂用',errors.getvalue())

    def test_explicit_home_and_code_directory_override_default(self):
        with mock.patch.dict(os.environ,{'NICHY_HOME':str(self.base/'explicit'),'NICHY_CODE_DIR':str(self.base/'code')}):
            self.assertEqual(nichy.queue_root(),(self.base/'explicit').resolve())
            self.assertEqual(nichy.queue_root(str(self.base/'argument')),(self.base/'argument').resolve())
        with mock.patch.dict(os.environ,{'NICHY_CODE_DIR':str(self.base/'code')},clear=True):
            self.assertEqual(nichy.queue_root(),(self.base/'code/start').resolve())
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = nichy.queue_root(str(self.base / 'queue'))
        self.project = self.base / 'project'
        self.project.mkdir()
        self.file = self.project / 'hello.py'
        self.file.write_text('print("version one")\n')

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, file=None, arguments=()):
        args = argparse.Namespace(file=str(file or self.file), file_args=list(arguments),
                                  timeout=10, wait=False, wait_limit=10)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch('nichy.time.monotonic', side_effect=[0, 5]):
            self.assertEqual(nichy.run_file(self.root, args), 0)
        return nichy.latest(self.root), out.getvalue()

    def test_offline_never_claims_machine_received(self):
        job, text = self.submit()
        self.assertIn('Mochi 记下了：hello.py · 版本 ', text)
        self.assertNotIn('Mochi 收到了', text)
        self.assertIn('机器还没读取', text)
        self.assertFalse((job / 'received.json').exists())

    def test_revision_stable_then_changes_and_snapshot_preserved(self):
        first, _ = self.submit()
        second, _ = self.submit()
        self.assertNotEqual(first, second)
        original = nichy.details(first)[0]['revision']
        self.assertEqual(original, nichy.details(second)[0]['revision'])
        self.file.write_text('print("version two")\n')
        third, _ = self.submit()
        self.assertNotEqual(original, nichy.details(third)[0]['revision'])
        self.assertIn('version one', (first/'code/hello.py').read_text())

    def test_special_filename_and_arguments_are_literal(self):
        file = self.project / "hello ; ignored.py"
        file.write_text('import sys, json\nprint(json.dumps(sys.argv[1:]))\n')
        arguments = ['a b', '$(touch SHOULD_NOT_EXIST)', '; exit 8']
        job, _ = self.submit(file, arguments)
        result = subprocess.run(['bash', str(job/'run.sh')], env=dict(os.environ,
                   GPUQ_CODE_DIR=str(job/'code'), NICHY_PYTHON=sys.executable),
                   text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertFalse((job/'code/SHOULD_NOT_EXIST').exists())

    def test_receipt_requires_exact_revision(self):
        job, _ = self.submit()
        spec = nichy.details(job)[0]
        nichy.core.write_json(job/'received.json', dict(state='READ', revision='other'))
        self.assertNotIn('Mochi 收到了', nichy.describe(job))
        nichy.core.write_json(job/'received.json', dict(state='READ', revision=spec['revision']))
        self.assertIn('Mochi 收到了：hello.py', nichy.describe(job))

    def test_expired_worker_does_not_claim_running(self):
        job, _ = self.submit()
        nichy.core.set_state(job, 'RUNNING')
        nichy.core.write_json(self.root/'worker.json', dict(state='RUNNING', updated_at=time.time()-60))
        self.assertNotIn('Mochi 正在运行', nichy.describe(job))
        self.assertIn('待确认', nichy.describe(job))

    def test_stop_targets_running_not_latest_pending(self):
        running, _ = self.submit()
        nichy.core.set_state(running, 'RUNNING')
        pending, _ = self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            nichy.stop_task(self.root, argparse.Namespace(id=None))
        self.assertTrue((running/'CANCEL').exists())
        self.assertFalse((pending/'CANCEL').exists())

    def test_queue_nested_in_project_is_pruned(self):
        self.root = nichy.queue_root(str(self.project/'queue'))
        first, _ = self.submit()
        second, _ = self.submit()
        self.assertEqual(nichy.details(first)[0]['source'], nichy.details(second)[0]['source'])
        self.assertFalse((second/'code/queue').exists())


class HeartbeatRulesTest(unittest.TestCase):
    def test_only_visible_empty_cool_devices_selected(self):
        cards = {name: dict(uuid=name, utilization=0, free_percent=99, processes=[])
                 for name in ['allocated', 'hidden', 'busy', 'full', 'used']}
        cards['busy']['utilization'] = 60
        cards['full']['free_percent'] = 40
        cards['used']['processes'] = [1234]
        self.assertEqual([x['uuid'] for x in eligible(cards, ['allocated','busy','full','used'])], ['allocated'])
        self.assertEqual(eligible(cards, []), [])

    def test_external_process_or_pressure_yields(self):
        card = dict(processes=[1], utilization=10, free_percent=98)
        self.assertFalse(demand(card))
        for change in [dict(processes=[1,2]), dict(utilization=90), dict(free_percent=20)]:
            self.assertTrue(demand(dict(card, **change)))

    def test_unknown_telemetry_fails_closed(self):
        result = argparse.Namespace(stdout='GPU-test, N/A, 100, 0\n')
        with mock.patch('keep_alive.subprocess.run', return_value=result):
            with self.assertRaises(ValueError):
                gpu_stats()

    def test_disabled_does_not_probe_cuda(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch('mochi_worker.subprocess.run') as probe:
            worker = NichyWorker(Path(directory), argparse.Namespace(), dict(enabled=False))
            self.assertEqual(worker.visible, [])
            self.assertIsNone(worker.idle_job(0))
            probe.assert_not_called()

    def test_invalid_configuration_rejected(self):
        for setting in [dict(enabled='false'), dict(seconds=121), dict(interval=0), dict(duty=.8), dict(idle_for=float('nan'))]:
            with self.assertRaises(ValueError):
                NichyWorker(Path('.'), argparse.Namespace(), setting)


if __name__ == '__main__':
    unittest.main(verbosity=2)
