"""Hardware-independent device selection and real child-process fault injection.

GPU values are simulated; these tests make no CUDA/NCCL performance claims.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import keep_alive
import mochi_core as core
import mochi_meeting as meeting
import mochi_worker as worker
from nichy import queue_root
from test_load import cards, LoadProcessTest


class PortabilityRulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = queue_root(str(Path(self.temp.name)/'queue'))
        meeting.prepare(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def query(self, devices, processes='', visible=None):
        replies = [argparse.Namespace(stdout=devices), argparse.Namespace(stdout=processes)]
        with mock.patch('keep_alive.subprocess.run', side_effect=replies):
            return keep_alive.gpu_stats(visible)

    def test_unallocated_unsupported_metrics_are_ignored(self):
        result = self.query('GPU-hidden, N/A, N/A, N/A\nGPU-mine, 5, 100, 1\n',
                            'GPU-hidden, N/A\n', ['GPU-mine'])
        self.assertEqual(list(result), ['GPU-mine'])
        self.assertEqual(result['GPU-mine']['free_percent'], 99)

    def test_allocated_unknown_invalid_or_missing_metrics_fail_closed(self):
        for row in ('N/A, 100, 1', 'nan, 100, 1', '101, 100, 1', '-1, 100, 1',
                    '0, 0, 0', '0, 100, 101', '0, 100, -1'):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.query('GPU-mine, '+row+'\n', visible=['GPU-mine'])
        with self.assertRaises(ValueError):
            self.query('GPU-hidden, 0, 100, 0\n', visible=['GPU-mine'])
        with self.assertRaises(ValueError):
            self.query('GPU-mine, 0, 100, 0\n'*2, visible=['GPU-mine'])

    def test_unavailable_process_telemetry_is_not_treated_as_empty(self):
        for pid in ('N/A', '0', '-1'):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                self.query('GPU-mine, 0, 100, 0\n', 'GPU-mine, '+pid+'\n', ['GPU-mine'])

    def test_empty_allocation_does_not_query_host_devices(self):
        with mock.patch('keep_alive.subprocess.run') as query:
            self.assertEqual(keep_alive.gpu_stats([]), {})
        query.assert_not_called()

    def test_uuid_mapping_survives_card_order_changes(self):
        result = self.query('GPU-b, 20, 24000, 100\nGPU-a, 80, 80000, 200\n',
                            'GPU-a, 12345\nGPU-b, 67890\n', ['GPU-a', 'GPU-b'])
        self.assertEqual(result['GPU-a']['processes'], [12345])
        self.assertEqual(result['GPU-b']['processes'], [67890])
        self.assertEqual(result['GPU-a']['utilization'], 80)

    def test_mig_identity_never_falls_back_to_whole_physical_card(self):
        with self.assertRaises(ValueError):
            self.query('GPU-parent, 0, 80000, 0\n', visible=['MIG-partition'])

    def test_probe_uses_current_runtime_and_preserves_visible_order(self):
        devices = ['b', 'a']
        cuda = argparse.Namespace(device_count=lambda:len(devices),
                                  get_device_properties=lambda i:argparse.Namespace(uuid=devices[i]))
        with mock.patch.dict(sys.modules, {'torch':argparse.Namespace(cuda=cuda)}):
            self.assertEqual(keep_alive.probe(), ['GPU-b', 'GPU-a'])
            devices[:] = ['new-host-card']
            self.assertEqual(keep_alive.probe(), ['GPU-new-host-card'])
            devices.clear()
            self.assertEqual(keep_alive.probe(), [])

    def test_new_machine_reprobes_instead_of_reusing_saved_uuids(self):
        core.write_json(self.root/'keep_alive.json', dict(gpu='GPU-old-machine'))
        w = worker.NichyWorker(self.root, argparse.Namespace(), {})
        self.assertEqual(w.visible, [])
        with mock.patch('mochi_worker.subprocess.run', return_value=argparse.Namespace(stdout='["GPU-new-machine"]')):
            w.probe_visible()
        self.assertEqual(w.visible, ['GPU-new-machine'])

    def test_one_unavailable_card_blocks_whole_group_for_1_2_4_8_cards(self):
        for count in (1, 2, 4, 8):
            for fault in ('process', 'memory', 'busy', 'missing'):
                with self.subTest(count=count, fault=fault):
                    w = worker.NichyWorker(self.root, argparse.Namespace(), {})
                    w.visible = ['GPU-%d'%i for i in range(count)]
                    observed = cards(w.visible)
                    last = observed[w.visible[-1]]
                    if fault == 'process': last['processes'] = [999999]
                    elif fault == 'memory': last['free_percent'] = 20
                    elif fault == 'busy': last['utilization'] = 80
                    else: observed.pop(w.visible[-1])
                    with mock.patch('mochi_worker.gpu_stats', return_value=observed) as sampler:
                        self.assertIsNone(w.idle_job(0))
                    sampler.assert_called_once_with(w.visible)
        self.assertFalse((self.root/'pulses').exists())

    def test_shared_queue_never_takes_over_another_machine_lock(self):
        lock = self.root/'worker.lock'
        lock.mkdir()
        owner = dict(host='another-machine', pid=1, token='original-owner', updated_at=0)
        core.write_json(lock/'owner.json', owner)
        w = worker.NichyWorker(self.root, argparse.Namespace(), {})
        with mock.patch('mochi_core.become_subreaper'), self.assertRaisesRegex(ValueError, '已有 worker'):
            w.run()
        self.assertEqual(core.read_json(lock/'owner.json'), owner)

    def test_shipped_350_helper_gets_separate_backup_without_touching_34(self):
        old = b'# shipped 3.5.0 fixture\n'
        (self.root/'keep_alive.py').write_bytes(old)
        (self.root/'keep_alive.py.v3.4.bak').write_bytes(b'older backup\n')
        with mock.patch.object(meeting, 'SHIPPED_HEARTBEATS', {hashlib.sha256(old).hexdigest():'3.5.0'}):
            meeting.prepare(self.root)
            meeting.prepare(self.root)
        self.assertEqual((self.root/'keep_alive.py.v3.5.0.bak').read_bytes(), old)
        self.assertEqual((self.root/'keep_alive.py.v3.4.bak').read_bytes(), b'older backup\n')
        self.assertEqual((self.root/'keep_alive.py').read_bytes(), Path(keep_alive.__file__).read_bytes())


class MultiCardProcessTest(LoadProcessTest):
    # Reuse fixtures only; run.py explicitly selects this class's own cases.
    def setUp(self):
        super().setUp()
        (self.root/'keep_alive.py').write_text('''import json,os,signal,sys,time
from pathlib import Path
job=Path(os.environ['GPUQ_JOB_DIR']);slot=os.environ['NICHY_LOAD_SLOT']
if os.environ.get('TEST_FAIL_SLOT')==slot: sys.exit(17)
if os.environ.get('TEST_STUBBORN_SLOT')==slot: signal.signal(signal.SIGTERM,signal.SIG_IGN)
if os.environ.get('TEST_SLOW_SLOT')==slot: time.sleep(1.5)
path=job/('load-'+slot+'-ready.json')
temp=path.with_suffix('.tmp')
temp.write_text(json.dumps({'pid':os.getpid(),'uuid':os.environ['CUDA_VISIBLE_DEVICES']}))
os.replace(temp,path)
for line in sys.stdin: float(line)
''')

    def test_1_2_4_8_children_match_allocation_and_all_exit(self):
        for count in (1, 2, 4, 8):
            with self.subTest(count=count):
                for path in self.root.glob('*.json'): path.unlink()
                self.uuids = ['GPU-%d'%i for i in reversed(range(count))]
                self.run_controller(seconds=1.2)
                metrics = core.read_json(self.root/'pulse-metrics.json')
                self.assertEqual([row['uuid'] for row in metrics['gpus']], self.uuids)
                for slot, uid in enumerate(self.uuids):
                    self.assertEqual(core.read_json(self.root/('load-%d-ready.json'%slot))['uuid'], uid)
                self.assertEqual(len(list(self.root.glob('load-*-ready.json'))), count)
                self.assert_children_stopped()

    def test_noncontiguous_subset_never_spawns_other_devices(self):
        self.uuids = ['GPU-7', 'GPU-2']
        self.run_controller()
        self.assertEqual(core.read_json(self.root/'pulse-ready.json')['uuids'], self.uuids)
        self.assertEqual(len(list(self.root.glob('load-*-ready.json'))), 2)
        self.assert_children_stopped()

    def test_heterogeneous_cards_have_independent_feedback(self):
        def sampler(visible=None):
            observed = self.telemetry()
            if all(card['processes'] for card in observed.values()):
                observed[self.uuids[0]]['utilization'] = 50
                observed[self.uuids[-1]]['utilization'] = 100
            return observed
        self.run_controller(sampler)
        rows = core.read_json(self.root/'pulse-metrics.json')['gpus']
        self.assertEqual(rows[0]['duty'], 1)
        self.assertLess(rows[-1]['duty'], 1)
        self.assert_children_stopped()

    def test_host_pid_namespace_never_changes_which_children_are_killed(self):
        def sampler(visible=None):
            observed = self.telemetry()
            for card in observed.values():
                card['processes'] = [pid+10000000 for pid in card['processes']]
            return observed
        self.run_controller(sampler)
        self.assertEqual(core.read_json(self.root/'pulse-result.json')['status'], 'complete')
        self.assert_children_stopped()

    def test_last_card_child_failure_cleans_other_seven(self):
        with mock.patch.dict(os.environ, {'TEST_FAIL_SLOT':'7'}), self.assertRaises(RuntimeError):
            self.run_controller(seconds=6)
        self.assertEqual(core.read_json(self.root/'pulse-result.json')['status'], 'child-failed')
        self.assert_children_stopped()

    def test_last_card_disappearing_stops_entire_group(self):
        def sampler(visible=None):
            observed = self.telemetry()
            if (self.root/'pulse-ready.json').exists(): observed.pop(self.uuids[-1])
            return observed
        self.run_controller(sampler, seconds=6)
        self.assertEqual(core.read_json(self.root/'pulse-result.json')['status'], 'telemetry-unavailable')
        self.assert_children_stopped()

    def test_memory_pressure_on_last_card_stops_entire_group(self):
        def sampler(visible=None):
            observed = self.telemetry()
            if (self.root/'pulse-ready.json').exists(): observed[self.uuids[-1]]['free_percent'] = 30
            return observed
        self.run_controller(sampler, seconds=6)
        self.assertEqual(core.read_json(self.root/'pulse-result.json')['status'], 'memory-pressure')
        self.assert_children_stopped()

    def test_slow_last_card_does_not_publish_premature_readiness(self):
        def sampler(visible=None):
            observed = self.telemetry()
            if not all(card['processes'] for card in observed.values()):
                self.assertFalse((self.root/'pulse-ready.json').exists())
                self.assertFalse((self.root/'pulse-metrics.json').exists())
            return observed
        with mock.patch.dict(os.environ, {'TEST_SLOW_SLOT':'7'}):
            self.run_controller(sampler, seconds=3.2)
        self.assertEqual(len(core.read_json(self.root/'pulse-metrics.json')['gpus']), 8)
        self.assert_children_stopped()

    def test_unresponsive_last_child_is_killed_before_controller_returns(self):
        original = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        with mock.patch.dict(os.environ, {'TEST_STUBBORN_SLOT':'7'}), mock.patch('mochi_load.subprocess.Popen', side_effect=spawn):
            self.run_controller()
        self.assertEqual(children[-1].returncode, -signal.SIGKILL)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assert_children_stopped()

    def test_external_process_on_last_card_survives_group_yield(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(20)'])
        try:
            def sampler(visible=None):
                observed = self.telemetry()
                if (self.root/'pulse-ready.json').exists(): observed[self.uuids[-1]]['processes'].append(other.pid)
                return observed
            self.run_controller(sampler, seconds=6)
            self.assertEqual(core.read_json(self.root/'pulse-result.json')['status'], 'external-demand')
            self.assertIsNone(other.poll())
            self.assert_children_stopped()
        finally:
            other.terminate()
            other.wait(5)
