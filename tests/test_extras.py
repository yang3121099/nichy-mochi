"""Deterministic Beijing-time, calendar, reminder and backup checks."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import mochi_core as core
import mochi_extras as extras
from mochi_meeting import MeetingPoint, prepare
from nichy import queue_root


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=extras.BEIJING).timestamp()


class ExtrasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = queue_root(str(Path(self.temp.name)/'queue'))
        prepare(self.root)
        self.messages = []
        self.reminders = extras.Reminders(self.root,self.messages.append)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, identity, when, body='echo hello\n'):
        script = Path(self.temp.name)/'command.sh'
        script.write_text(body)
        args = argparse.Namespace(script=str(script),id=identity,cwd=str(script.parent),timeout=10,
            source=None,snapshot_limit_mib=100,background=False,resume_safe=False,max_preemptions=0,label='command.sh')
        with mock.patch('mochi_core.time.time',return_value=at(when)):
            core.submit(self.root,args)
        return self.root/'jobs'/identity

    def meeting(self):
        return MeetingPoint(argparse.Namespace(root=self.root))

    def test_fixed_beijing_time_without_offset(self):
        self.assertEqual(extras.timestamp(at('2026-09-17 00:00:01')),'2026-09-17 00:00:01')
        self.assertEqual(extras.submission_hour(at('2026-09-17 00:00:01')),'2026-09-17_00')

    def test_hourly_count_midnight_and_restart(self):
        self.submit('one','2026-09-17 23:10:00')
        self.submit('two','2026-09-17 23:59:00')
        self.meeting().register_submissions()
        self.submit('three','2026-09-18 00:00:00')
        self.meeting().register_submissions()
        records=core.read_json(self.root/'.submissions.json')
        self.assertEqual([records[n]['hour_number'] for n in ['one','two','three']],[1,2,1])
        self.assertEqual(records['three']['hour'],'2026-09-18_00')
        self.assertEqual([records[n]['number'] for n in ['one','two','three']],[1,2,3])
        self.assertEqual(extras.submission_title(records['three']),'2026-09-18 00时 · 第 1 次提交')

    def test_backup_exact_bytes_distinct_names_and_restart(self):
        old=self.submit('one','2026-09-17 14:00:00','echo ORIGINAL\n')
        self.meeting().register_submissions()
        self.submit('two','2026-09-17 14:01:00','echo NEW\n')
        self.meeting().register_submissions()
        first=self.root/'backups/command_2026-09-17_14h_001.sh'
        second=self.root/'backups/command_2026-09-17_14h_002.sh'
        self.assertEqual(first.read_bytes(),(old/'run.sh').read_bytes())
        self.assertEqual(second.read_text(),'echo NEW\n')
        self.meeting().register_submissions()
        self.assertEqual(len(list((self.root/'backups').iterdir())),2)
        self.assertIn(first.name,(self.root/'submissions.tsv').read_text())

    def test_registry_migration_and_backup_recovery(self):
        job=self.submit('one','2026-09-17 14:00:00')
        spec=core.read_json(job/'request.json')
        core.write_json(self.root/'.submissions.json',{'one':dict(number=12,label='command.sh',revision=spec['revision'],
            submitted_at=spec['submitted_at'],job=str(job))})
        meeting=self.meeting()
        with mock.patch.object(meeting,'backup_commands',side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):meeting.register_submissions()
        self.assertEqual(core.read_json(self.root/'.submissions.json')['one']['hour_number'],1)
        self.meeting().register_submissions()
        record=core.read_json(self.root/'.submissions.json')['one']
        self.assertEqual(record['number'],12)
        self.assertEqual((self.root/record['backup']).read_text(),'echo hello\n')

    def test_changed_backup_is_not_overwritten(self):
        self.submit('one','2026-09-17 14:00:00')
        meeting=self.meeting()
        with mock.patch.object(meeting,'backup_commands',side_effect=OSError('before copy')):
            with self.assertRaises(OSError):meeting.register_submissions()
        directory=self.root/'backups';directory.mkdir()
        target=directory/'command_2026-09-17_14h_001.sh';target.write_text('existing file\n')
        with self.assertRaises(ValueError):self.meeting().register_submissions()
        self.assertEqual(target.read_text(),'existing file\n')

    def test_night_reminder_once_across_restart(self):
        self.reminders.poll(at('2026-09-17 22:29:00'));self.assertEqual(self.messages,[])
        self.reminders.poll(at('2026-09-17 22:30:00'))
        self.reminders.poll(at('2026-09-17 22:31:00'))
        extras.Reminders(self.root,self.messages.append).poll(at('2026-09-17 22:32:00'))
        self.assertEqual(len(self.messages),1)
        self.assertIn('该下班了',self.messages[0])

    def test_friday_three_slots_and_no_old_message_flood(self):
        for clock in ['09:00:00','12:00:00','17:00:00']:
            self.reminders.poll(at('2026-09-18 '+clock))
        self.assertEqual(len(self.messages),3)
        self.assertTrue(all(message.count('终于周五啦')==3 for message in self.messages))
        self.reminders.poll(at('2026-09-18 19:00:00'))
        self.assertEqual(len(self.messages),3)

    def test_official_holiday_and_adjusted_workday(self):
        self.reminders.poll(at('2026-09-25 09:00:00'))
        self.assertTrue(any('今天节假日' in message for message in self.messages))
        self.messages.clear()
        self.reminders.poll(at('2026-09-20 09:00:00'))
        self.assertEqual(self.messages,[])
        self.assertIn('该下班了',self.reminders.command_suffix(at('2026-09-20 22:30:00')))
        self.assertEqual(self.reminders.command_suffix(at('2026-09-19 22:30:00')),'')

    def test_command_suffixes_follow_submission_date(self):
        self.assertIn('终于周五啦',self.reminders.command_suffix(at('2026-09-18 01:00:00')))
        self.assertIn('今天节假日',self.reminders.command_suffix(at('2026-10-01 02:00:00')))
        suffix=self.reminders.command_suffix(at('2026-09-18 22:30:00'))
        self.assertIn('终于周五啦',suffix);self.assertIn('该下班了',suffix)
        self.assertNotIn('该下班了',self.reminders.command_suffix(at('2026-09-18 22:29:59')))
        self.submit('late','2026-09-18 23:00:00')
        self.meeting().register_submissions()
        self.assertEqual(core.read_json(self.root/'.submissions.json')['late']['note'],suffix)

    def test_unknown_year_and_invalid_calendar_do_not_guess(self):
        self.reminders.poll(at('2027-01-01 09:00:00'))
        self.assertTrue(any('日历待更新' in message for message in self.messages))
        self.assertFalse(any('今天节假日' in message for message in self.messages))
        (self.root/'holidays.json').write_text('{bad')
        other=extras.Reminders(self.root,self.messages.append)
        other.poll(at('2026-09-17 22:30:00'))
        self.assertTrue(any('该下班了' in message for message in self.messages))
        self.assertTrue(any('日历需要检查' in message for message in self.messages))
