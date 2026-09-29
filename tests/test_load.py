"""Portable controller/process tests; utilization samples here are simulated."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import mochi_load as load
import mochi_meeting as meeting
import mochi_worker as worker
from mochi_core import read_json
from mochi_meeting import prepare
from nichy import queue_root


def cards(uuids, utilization=0):
    return {uid:dict(uuid=uid,utilization=utilization,free_percent=99,processes=[]) for uid in uuids}


class LoadRulesTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=queue_root(str(Path(self.temp.name)/'queue'))
        prepare(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_defaults_and_exact_legacy_defaults_upgrade(self):
        for configuration in ({},worker.LEGACY_DEFAULTS):
            result=worker.checked_config(configuration)
            self.assertEqual(result['mode'],'continuous')
            self.assertEqual(result['target_utilization'],90)
            self.assertEqual(result['idle_for'],0)
            self.assertEqual(result['interval'],0)
        self.assertFalse(worker.checked_config(dict(worker.LEGACY_DEFAULTS,enabled=False))['enabled'])
        self.assertEqual(worker.checked_config(dict(seconds=2,duty=.1))['mode'],'pulse')
        self.assertEqual(worker.checked_config(dict(mode='pulse'))['duty'],.25)

    def test_continuous_config_validation(self):
        for change in (dict(mode='other'),dict(target_utilization=75),dict(target_utilization=101),
                       dict(target_utilization=float('inf')),dict(seconds=86401),dict(duty=1.1),dict(duty=True)):
            configuration=dict(mode='continuous');configuration.update(change)
            with self.assertRaises(ValueError):worker.checked_config(configuration)

    def test_all_visible_cards_start_without_thirty_second_delay(self):
        w=worker.NichyWorker(self.root,argparse.Namespace(),{})
        w.visible=['GPU-a','GPU-b']
        with mock.patch('mochi_worker.gpu_stats',return_value=cards(w.visible)),mock.patch.object(w,'flags',return_value=(False,False)),mock.patch.object(w,'pending',return_value=[]):
            job=w.idle_job(0)
        spec=read_json(job/'request.json')
        self.assertEqual(spec['pulse_gpu_uuid'],'GPU-a,GPU-b')
        self.assertIn('--continuous',(job/'run.sh').read_text())
        self.assertIn('--target 90',(job/'run.sh').read_text())

    def test_one_external_card_blocks_whole_idle_group(self):
        w=worker.NichyWorker(self.root,argparse.Namespace(),{})
        w.visible=['GPU-a','GPU-b']
        busy=cards(w.visible);busy['GPU-b']['processes']=[123]
        with mock.patch('mochi_worker.gpu_stats',return_value=busy):
            self.assertIsNone(w.idle_job(0))
        self.assertFalse((self.root/'pulses').exists())

    def test_pending_task_and_incoming_file_preempt_idle(self):
        w=worker.NichyWorker(self.root,argparse.Namespace(),{})
        w.visible=['GPU-a']
        with mock.patch('mochi_worker.gpu_stats',return_value=cards(w.visible)),mock.patch.object(w,'flags',return_value=(False,False)),mock.patch.object(w,'pending',return_value=[object()]):
            self.assertIsNone(w.idle_job(0))
        w.meeting.command_candidate=('upload',0)
        self.assertIsNone(w.idle_job(0))
        self.assertTrue(w.extra_preempt(dict(pulse=True)))
        self.assertFalse(w.extra_preempt(dict(pulse=False)))

    def test_own_high_utilization_is_not_mistaken_for_external_demand(self):
        observed=cards(['GPU-a'],100);observed['GPU-a']['processes']=[1000]
        self.assertIsNone(load.busy_reason(observed,['GPU-a'],{'GPU-a':1000}))
        observed['GPU-a']['processes']=[2000]
        self.assertEqual(load.busy_reason(observed,['GPU-a'],{'GPU-a':1000}),'external-demand')
        self.assertEqual(load.busy_reason({},['GPU-a'],{}),'telemetry-unavailable')

    def test_feedback_adjusts_both_ways_and_is_bounded(self):
        self.assertGreater(load.next_duty(.8,65,90),.8)
        self.assertLess(load.next_duty(.9,100,90),.9)
        self.assertEqual(load.next_duty(1,0,90),1)
        self.assertGreaterEqual(load.next_duty(.5,100,90),.5)

    def test_unknown_telemetry_never_starts_load(self):
        w=worker.NichyWorker(self.root,argparse.Namespace(),{})
        w.visible=['GPU-a']
        with mock.patch('mochi_worker.gpu_stats',side_effect=ValueError('N/A')):
            self.assertIsNone(w.idle_job(0))
        self.assertEqual(read_json(self.root/'keep_alive.json')['state'],'telemetry-unavailable')

    def test_invalid_probe_never_expands_gpu_visibility(self):
        for value in ('GPU-a',None,[1],['GPU-a','GPU-a']):
            w=worker.NichyWorker(self.root,argparse.Namespace(),{})
            with mock.patch('mochi_worker.subprocess.run',return_value=argparse.Namespace(stdout=json.dumps(value))):
                w.probe_visible()
            self.assertEqual(w.visible,[])
            self.assertEqual(w.heartbeat_state,'unavailable')

    def test_known_old_helper_upgrades_with_exact_backup(self):
        old=b'# old shipped helper fixture\n'
        (self.root/'keep_alive.py').write_bytes(old)
        with mock.patch.object(meeting,'SHIPPED_HEARTBEATS',{hashlib.sha256(old).hexdigest()}):
            prepare(self.root)
            prepare(self.root)
        self.assertEqual((self.root/'keep_alive.py.v3.4.bak').read_bytes(),old)
        self.assertEqual((self.root/'keep_alive.py').read_bytes(),Path(meeting.__file__).with_name('keep_alive.py').read_bytes())

    def test_custom_helper_and_existing_configuration_are_preserved(self):
        custom=b'# my own GPU helper\n'
        (self.root/'keep_alive.py').write_bytes(custom)
        (self.root/'config.json').write_text('{"enabled":false}')
        prepare(self.root)
        self.assertEqual((self.root/'keep_alive.py').read_bytes(),custom)
        self.assertEqual((self.root/'config.json').read_text(),'{"enabled":false}')


class LoadProcessTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.uuids=['GPU-%d'%i for i in range(8)]
        # Real subprocess lifetimes and pipe/termination handling, fake GPU work.
        (self.root/'keep_alive.py').write_text('''import json,os,sys,time
from pathlib import Path
job=Path(os.environ['GPUQ_JOB_DIR']);slot=os.environ['NICHY_LOAD_SLOT']
(job/('load-'+slot+'-ready.json')).write_text(json.dumps({'pid':os.getpid()}))
for line in sys.stdin:
    float(line)
''')

    def tearDown(self):
        self.temp.cleanup()

    def telemetry(self):
        result=cards(self.uuids,95)
        for slot,uid in enumerate(self.uuids):
            path=self.root/('load-%d-ready.json'%slot)
            if path.exists():
                result[uid]['processes']=[json.loads(path.read_text())['pid']]
            else:result[uid]['utilization']=0
        return result

    def run_controller(self, sampler=None, seconds=1.2):
        with mock.patch.dict(os.environ,{'GPUQ_JOB_DIR':str(self.root),'CUDA_VISIBLE_DEVICES':','.join(self.uuids)}),mock.patch.object(load,'__file__',str(self.root/'mochi_load.py')),mock.patch.object(load,'gpu_stats',side_effect=sampler or self.telemetry),mock.patch.object(load.signal,'signal'):
            load.continuous(self.uuids,seconds,90)

    def assert_children_stopped(self):
        for path in self.root.glob('load-*-ready.json'):
            with self.assertRaises(ProcessLookupError):os.kill(json.loads(path.read_text())['pid'],0)

    def test_eight_children_complete_and_report_per_card_samples(self):
        self.run_controller()
        metrics=read_json(self.root/'pulse-metrics.json')
        self.assertEqual(len(metrics['gpus']),8)
        self.assertEqual(metrics['average'],95)
        self.assertEqual(metrics['scope'],'idle-only-last-60-samples')
        self.assertTrue(read_json(self.root/'pulse-result.json')['children_stopped'])
        self.assert_children_stopped()

    def test_external_process_is_never_killed(self):
        other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
        try:
            def sampler():
                result=self.telemetry()
                if (self.root/'pulse-ready.json').exists():
                    result[self.uuids[0]]['processes'].append(other.pid)
                return result
            self.run_controller(sampler,seconds=8)
            self.assertEqual(read_json(self.root/'pulse-result.json')['status'],'external-demand')
            self.assertIsNone(other.poll())
            self.assert_children_stopped()
        finally:
            other.terminate();other.wait(5)

    def test_telemetry_failure_cleans_every_child(self):
        def sampler():
            if (self.root/'pulse-ready.json').exists():raise ValueError('simulated telemetry failure')
            return self.telemetry()
        with self.assertRaises(RuntimeError):self.run_controller(sampler,seconds=8)
        self.assert_children_stopped()

    def test_allocated_visibility_must_match(self):
        with mock.patch.dict(os.environ,{'CUDA_VISIBLE_DEVICES':'GPU-other'}):
            with self.assertRaises(ValueError):load.continuous(self.uuids,1,90)
