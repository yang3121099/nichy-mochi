import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "mochi_core.py"


class QueueTest(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get("GPUQ_TEST_BASE", str(SCRIPT.parent / "tests")))
        parent.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=parent))
        self.client_python = os.environ.get("GPUQ_CLIENT_PYTHON", sys.executable)
        self.client_env = dict(PATH="/usr/bin:/bin", LANG="C.UTF-8", CUDA_VISIBLE_DEVICES="", GPUQ_SIDE="CPU")
        self.root = self.base / "queue"
        self.workers = []
        self.logs = []

    def tearDown(self):
        for p in self.workers:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(8)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        for f in self.logs:
            f.seek(0)
            content = f.read()
            if "错误" in content or "Traceback" in content:
                print(content)
            f.close()
        if os.environ.get("GPUQ_KEEP_TESTS") != "1":
            import shutil
            shutil.rmtree(self.base)

    def cli(self, *args, ok=True):
        command = [self.client_python, str(SCRIPT), "--root", str(self.root), *args]
        p = subprocess.run(command, env=self.client_env, cwd=self.base,
                           text=True, capture_output=True, timeout=10)
        with (self.base / "client-events.jsonl").open("a") as stream:
            stream.write(json.dumps(dict(time=time.time(), command=command, returncode=p.returncode,
                                         stdout=p.stdout, stderr=p.stderr)) + "\n")
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def wait_for(self, check, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.02)
        self.fail("condition timed out")

    def state(self, job):
        p = self.root / "jobs" / job / "status.json"
        return json.loads(p.read_text())["state"]

    def worker(self):
        f = (self.base / ("worker-%s.log" % len(self.workers))).open("w+")
        self.logs.append(f)
        p = subprocess.Popen([sys.executable, str(SCRIPT), "--root", str(self.root),
                              "worker", "--poll", ".02", "--grace", ".1", "--idle-exit", "30"],
                             stdout=f, stderr=f)
        self.workers.append(p)
        self.wait_for(lambda: (self.root / "worker.json").exists() and json.loads((self.root / "worker.json").read_text()).get('pid') == p.pid)
        return p

    def submit(self, name, body="echo hello\n", timeout="10"):
        path = self.base / (name + ".sh")
        path.write_text(body)
        self.cli("submit", str(path), "--id", name, "--cwd", str(self.base), "--timeout", timeout)
        return path

    def test_publish_snapshot_deduplicate_and_serial_failure_continuation(self):
        script = self.submit("one", "echo original\nsleep .1\n")
        self.cli("submit", str(script), "--id", "one", "--cwd", str(self.base), "--timeout", "10")
        script.write_text("echo changed\n")
        self.assertNotEqual(self.cli("submit", str(script), "--id", "one", "--cwd", str(self.base), ok=False).returncode, 0)
        self.submit("two", "exit 7\n")
        self.submit("three", "echo final\n")
        self.worker()
        self.wait_for(lambda: self.state("three") == "SUCCEEDED")
        self.assertEqual(self.state("one"), "SUCCEEDED")
        self.assertEqual(self.state("two"), "FAILED")
        first = json.loads((self.root / "jobs/one/status.json").read_text())
        second = json.loads((self.root / "jobs/two/status.json").read_text())
        self.assertLessEqual(first["finished_at"], second["started_at"])
        self.assertIn("original", self.cli("logs", "one").stdout)
        self.assertNotIn("changed", self.cli("logs", "one").stdout)
        self.assertEqual(list((self.root / "staging").iterdir()), [])

    def test_cancel_pending(self):
        self.submit("cancelled", "touch SHOULD_NOT_EXIST\n")
        self.cli("cancel", "cancelled")
        self.worker()
        self.wait_for(lambda: self.state("cancelled") == "CANCELLED")
        self.assertFalse((self.base / "SHOULD_NOT_EXIST").exists())

    def test_concurrent_same_id_publishes_once(self):
        script = self.base / "concurrent.sh"
        script.write_text("echo once\n")
        command = [self.client_python, str(SCRIPT), "--root", str(self.root), "submit",
                   str(script), "--id", "shared", "--cwd", str(self.base)]
        clients = [subprocess.Popen(command, env=self.client_env, cwd=self.base, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True) for _ in range(6)]
        for client in clients:
            out, err = client.communicate(timeout=8)
            self.assertEqual(client.returncode, 0, err)
            self.assertEqual(out.strip(), "shared")
        self.assertEqual(len(list((self.root / "jobs").iterdir())), 1)
        self.worker()
        self.wait_for(lambda: self.state("shared") == "SUCCEEDED")
        self.assertEqual(self.cli("logs", "shared").stdout.strip(), "once")

    def test_running_cancel_cleans_children(self):
        self.submit("long", "sleep 30 &\nchild=$!\necho $child > child.pid\nwait $child\n")
        self.worker()
        self.wait_for(lambda: (self.base / "child.pid").exists())
        pid = int((self.base / "child.pid").read_text())
        self.cli("cancel", "long")
        self.wait_for(lambda: self.state("long") == "CANCELLED")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_and_next_task(self):
        self.submit("timeout", "sleep 30\n", timeout=".15")
        self.submit("after", "echo after\n")
        self.worker()
        self.wait_for(lambda: self.state("after") == "SUCCEEDED")
        self.assertEqual(self.state("timeout"), "TIMED_OUT")

    def test_drain_preserves_next_pending_and_restart(self):
        self.submit("current", "touch started\nsleep .4\n")
        self.submit("later")
        worker = self.worker()
        self.wait_for(lambda: (self.base / "started").exists())
        self.cli("stop")
        self.assertEqual(worker.wait(5), 0)
        self.assertEqual(self.state("current"), "SUCCEEDED")
        self.assertEqual(self.state("later"), "PENDING")
        self.assertFalse((self.root / "worker.lock").exists())
        self.worker()
        self.wait_for(lambda: self.state("later") == "SUCCEEDED")

    def test_stop_now_and_single_worker(self):
        self.submit("long", "sleep 30\n")
        worker = self.worker()
        self.wait_for(lambda: self.state("long") == "RUNNING")
        self.assertNotEqual(self.cli("worker", ok=False).returncode, 0)
        self.cli("stop", "--now")
        self.assertEqual(worker.wait(6), 0)
        self.assertEqual(self.state("long"), "CANCELLED")

    def test_sigterm_cleanup(self):
        self.submit("long", "touch started\nsleep 30\n")
        worker = self.worker()
        self.wait_for(lambda: (self.base / "started").exists())
        worker.terminate()
        self.assertEqual(worker.wait(6), 0)
        self.assertEqual(self.state("long"), "INTERRUPTED")

    def test_unpublished_is_ignored_and_recovery_never_reexecutes(self):
        self.submit("unknown", "touch SHOULD_NOT_EXIST\n")
        unfinished = self.root / "staging/partial"
        unfinished.mkdir()
        (unfinished / "run.sh").write_text("touch SHOULD_NOT_EXIST\n")
        (self.root / "jobs/unknown/status.json").write_text('{"state":"RUNNING"}')
        worker = self.worker()
        self.assertNotEqual(worker.wait(5), 0)
        self.assertTrue((self.root / "worker.lock").exists())
        self.cli("recover", "--confirm-old-worker-stopped")
        self.assertEqual(self.state("unknown"), "UNKNOWN")
        self.assertFalse((self.root / "worker.lock").exists())
        worker = self.worker()
        time.sleep(.15)
        self.assertFalse((self.base / "SHOULD_NOT_EXIST").exists())
        self.cli("stop")
        self.assertEqual(worker.wait(5), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
