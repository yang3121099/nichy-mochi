"""Portable client tests; no Linux/GPU claim."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'mochi_core.py'

class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'queue'
    def tearDown(self):
        self.tmp.cleanup()
    def cli(self, *args, body=None, ok=True):
        r = subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root), *args],
                           input=body, text=True, capture_output=True, timeout=10)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r
    def submit(self, *args, body='echo hi\n', ok=True):
        return self.cli('submit', '-', '--id', 'a', '--cwd', str(self.base), *args, body=body, ok=ok)
    def test_atomic_stdin_snapshot(self):
        self.submit()
        self.assertEqual((self.root/'jobs/a/run.sh').read_text(), 'echo hi\n')
        self.assertEqual(list((self.root/'staging').iterdir()), [])
        self.assertEqual(json.loads((self.root/'jobs/a/status.json').read_text())['state'], 'PENDING')
    def test_duplicate_and_conflict(self):
        self.submit(); self.submit()
        self.assertNotEqual(self.submit(body='different', ok=False).returncode, 0)
    def test_concurrent_same_id(self):
        command = [sys.executable,str(SCRIPT),'--root',str(self.root),'submit','-', '--id','a','--cwd',str(self.base)]
        ps = [subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(12)]
        for p in ps:
            out,err = p.communicate('echo hi\n', timeout=10)
            self.assertEqual(p.returncode, 0, err); self.assertEqual(out.strip(),'a')
        self.assertEqual(len(list((self.root/'jobs').iterdir())),1)
    def test_source_snapshot_dedup_and_change(self):
        src=self.base/'src';src.mkdir();(src/'train.py').write_text('print(1)')
        self.submit('--source',str(src));self.submit('--source',str(src))
        (src/'train.py').write_text('print(2)')
        self.assertNotEqual(self.submit('--source',str(src),ok=False).returncode,0)
        self.assertEqual((self.root/'jobs/a/code/train.py').read_text(),'print(1)')
    def test_source_symlinks_rejected(self):
        src=self.base/'src';src.mkdir();(src/'link').symlink_to('/etc/passwd')
        self.assertNotEqual(self.submit('--source',str(src),ok=False).returncode,0)
        self.assertFalse((self.root/'jobs/a').exists())
    def test_source_size_limit(self):
        src=self.base/'src';src.mkdir();(src/'large').write_bytes(b'x'*2048)
        self.assertNotEqual(self.submit('--source',str(src),'--snapshot-limit-mib','.001',ok=False).returncode,0)
        self.assertEqual(list((self.root/'staging').iterdir()),[])
    def test_background_identity(self):
        self.submit('--background','--resume-safe')
        self.assertNotEqual(self.submit(ok=False).returncode,0)
    def test_invalid_arguments(self):
        for args in [('--timeout','nan'),('--timeout','0'),('--id','../../escape'),('--resume-safe',),('--max-preemptions','-1')]:
            self.assertNotEqual(self.submit(*args,ok=False).returncode,0)
    def test_cancel_and_json(self):
        self.submit();self.cli('cancel','a')
        self.assertTrue((self.root/'jobs/a/CANCEL').exists())
        d=json.loads(self.cli('status','a','--json').stdout)
        self.assertEqual(d['jobs']['a']['state'],'PENDING')
    def test_wait_exit_codes(self):
        self.submit();self.assertEqual(self.cli('wait','a','--deadline','0',ok=False).returncode,2)
        for state,code in [('SUCCEEDED',0),('FAILED',1),('PREEMPTED',1)]:
            (self.root/'jobs/a/status.json').write_text(json.dumps({'state':state}))
            self.assertEqual(self.cli('wait','a',ok=False).returncode,code)

if __name__ == '__main__':
    unittest.main(verbosity=2)
