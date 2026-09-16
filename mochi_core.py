#!/usr/bin/env python3
"""Small Linux shared-filesystem job queue. Internal queue engine for NichyMochi.

Public entry point: nichy run hello.py / nichy serve.
Requires Python >= 3.9, Linux, bash and a POSIX shared filesystem.
"""
import argparse
import collections
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import select

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED", "UNKNOWN", "PREEMPTED"}
VERSION = "3.3.0"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path, data):
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
        sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def valid_id(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ValueError("任务 ID 仅允许字母、数字、下划线和短横线，长度 1–96。")
    return value


def job_path(root, job_id):
    path = root / "jobs" / valid_id(job_id)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("任务不存在：" + job_id)
    return path


def set_state(job, state, **extra):
    write_json(job / "status.json", dict(state=state, updated_at=time.time(), **extra))
    sync_dir(job)


def jobs(root):
    return sorted(p for p in (root / "jobs").iterdir() if p.is_dir() and not p.is_symlink())


def source_manifest(directory):
    entries = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("代码快照不接受符号链接：" + str(path))
        if path.is_file():
            entries[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError("代码快照只接受普通文件和目录")
    return entries


def source_paths(directory, queue_root):
    """Prune queue/cache/output trees before walking, so a project can contain its queue."""
    excluded = {".git", ".venv", "venv", "__pycache__", ".nichy", ".mochi", "outputs"}
    for base, dirs, files in os.walk(directory, followlinks=False):
        kept = []
        for name in sorted(dirs):
            path = Path(base) / name
            if name in excluded or path.resolve() == queue_root.resolve():
                continue
            if path.is_symlink():
                raise ValueError("代码目录不接受符号链接")
            kept.append(name)
            yield path
        dirs[:] = kept
        for name in sorted(files):
            yield Path(base) / name


def validate_payload(job, spec):
    if hashlib.sha256((job / "run.sh").read_bytes()).hexdigest() != spec["sha256"]:
        raise ValueError("已发布脚本内容被修改")
    if spec.get("source") is not None and source_manifest(job / "code") != spec["source"]:
        raise ValueError("已发布代码快照被修改")


def submit(root, args):
    body = sys.stdin.buffer.read() if args.script == "-" else Path(args.script).read_bytes()
    job_id = valid_id(args.id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12])
    cwd = os.path.abspath(args.cwd)
    spec = {"cwd": cwd, "timeout": args.timeout, "sha256": hashlib.sha256(body).hexdigest(),
            "background": args.background, "resume_safe": args.resume_safe,
            "max_preemptions": args.max_preemptions, "version": VERSION, "source": None,
            "label": getattr(args,"label",None) or (Path(args.script).name if args.script!="-" else "任务")}
    destination = root / "jobs" / job_id
    stage = root / "staging" / (job_id + "-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        if args.source:
            source = Path(args.source).resolve(strict=True)
            if not source.is_dir() or root.resolve() == source:
                raise ValueError("--source 必须为目录，且不可等于队列根目录")
            target = stage / "code"
            target.mkdir()
            total = 0
            for path in source_paths(source, root):
                rel = path.relative_to(source)
                if any(part in {".git", ".venv", "__pycache__"} for part in rel.parts):
                    continue
                if path.is_symlink():
                    raise ValueError("--source 不接受符号链接；数据和模型请通过共享绝对路径引用")
                dest = target / rel
                if path.is_dir():
                    dest.mkdir(exist_ok=True)
                elif path.is_file():
                    size = path.stat().st_size
                    total += size
                    if total > args.snapshot_limit_mib * 1024 * 1024:
                        raise ValueError("代码快照超出大小上限；不要打包模型、数据或环境")
                    with path.open("rb") as stream:
                        data = stream.read(size + 1)
                    if len(data) != size:
                        raise ValueError("源文件复制时大小发生变化；请停止修改源码后重试")
                    atomic_write(dest, data)
                    if path.stat().st_mode & 0o111:
                        dest.chmod(0o700)
                else:
                    raise ValueError("源码中存在非普通文件")
            spec["source"] = source_manifest(target)
        spec["revision"] = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        def same_request():
            old = read_json(destination / "request.json")
            if any(old.get(key) != value for key, value in spec.items()):
                raise ValueError("同名任务内容不同，请换一个 --id；旧任务未被修改。")
            print(job_id)
        if destination.exists():
            same_request()
            return
        atomic_write(stage / "run.sh", body)
        write_json(stage / "request.json", dict(spec, submitted_at=time.time(), id=job_id))
        set_state(stage, "PENDING", revision=spec["revision"], label=spec["label"])
        try:
            os.rename(str(stage), str(destination))
        except OSError:
            if destination.exists():
                same_request()
                return
            raise
        sync_dir(root / "jobs")
        print(job_id)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def process_stamp(pid):
    """PID plus Linux start ticks; never infer ownership from a numeric PID alone."""
    try:
        return Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def descendants():
    found, todo = {}, [os.getpid()]
    while todo:
        pid = todo.pop()
        try:
            children = Path('/proc/{0}/task/{0}/children'.format(pid)).read_text().split()
        except OSError:
            continue
        for value in children:
            child = int(value)
            if child not in found:
                found[child] = process_stamp(child)
                todo.append(child)
    return found


def clean_descendants(proc, grace):
    """Dedicated subreaper owns only one job, including reparented setsid children."""
    for sig, allowance in [(signal.SIGTERM, grace), (signal.SIGKILL, 5.0)]:
        deadline = time.monotonic() + allowance
        while True:
            proc.poll()
            while True:
                try:
                    child, result = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not child:
                    break
                if child == proc.pid:
                    proc.returncode = os.waitstatus_to_exitcode(result) if hasattr(os, 'waitstatus_to_exitcode') else (os.WEXITSTATUS(result) if os.WIFEXITED(result) else -os.WTERMSIG(result))
            owned = descendants()
            if not owned:
                return
            for pid, stamp in owned.items():
                if stamp is not None and process_stamp(pid) == stamp:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                break
            time.sleep(.05)
    raise RuntimeError("守护进程无法清理所有子进程；停止队列以避免并行污染")


def supervise(job, cwd, grace, token, limit):
    """Pipe EOF catches worker SIGKILL; no shared-disk reads during supervision."""
    become_subreaper()
    stopping = [False]
    def stop(*_):
        stopping[0] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    proc, code, error = None, None, ""
    deadline = time.monotonic() + limit
    timed_out = False
    try:
        readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
        if readable and not os.read(sys.stdin.fileno(), 1):
            stopping[0] = True
            return 1
        proc = subprocess.Popen(["bash", "-e", "-o", "pipefail", str(job / "run.sh")],
                                cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=True)
        while proc.poll() is None and not stopping[0]:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], .05)
            if readable and not os.read(sys.stdin.fileno(), 1):
                stopping[0] = True
        code = proc.poll()
    except Exception as exc:
        error = repr(exc)
    finally:
        if proc is not None:
            clean_descendants(proc, grace)
        write_json(job / ("guard-" + token + ".json"), dict(cleaned=True, returncode=code,
                   interrupted=stopping[0], timed_out=timed_out, error=error, finished_at=time.time()))
    return 0 if code == 0 and not error else 1


def become_subreaper():
    # Adopt this worker's orphaned descendants so cancelled jobs leave no zombies.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, ctypes.c_ulong(1), ctypes.c_ulong(0),
                  ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, "当前容器不允许进程收尾所需的 prctl：" + os.strerror(error))


class Worker:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.lock = root / "worker.lock"
        self.identity = dict(token=uuid.uuid4().hex, host=socket.gethostname(),
                             pid=os.getpid(), started_at=time.time(),
                             process_stamp=process_stamp(os.getpid()), version=VERSION)
        self.state, self.current, self.proc = "STARTING", None, None
        self.interrupted, self.last_beat = False, 0.0
        self.born = time.monotonic()
        self.guard_token = None
        self.last_receipts = 0.0

    def acknowledge(self):
        if time.monotonic() - self.last_receipts < .5:
            return
        self.last_receipts = time.monotonic()
        for job in jobs(self.root):
            if read_json(job / "status.json")["state"] != "PENDING":
                continue
            receipt = job / "received.json"
            if receipt.exists() and read_json(receipt).get("worker_token") == self.identity["token"]:
                continue
            try:
                spec = read_json(job / "request.json")
                validate_payload(job, spec)
                write_json(receipt, dict(state="READ", revision=spec.get("revision"),
                           worker_token=self.identity["token"], received_at=time.time()))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                write_json(receipt, dict(state="REJECTED", detail=str(exc),
                           worker_token=self.identity["token"], received_at=time.time()))
                set_state(job, "FAILED", detail=str(exc), finished_at=time.time())

    def idle_job(self, idle_since):
        return None

    def extra_preempt(self, spec):
        return False

    def heartbeat(self, force=False):
        if self.state not in {'BLOCKED', 'STOPPED'}:
            self.acknowledge()
        if force or time.monotonic() - self.last_beat >= 5:
            current = {}
            if self.current:
                try:
                    current = read_json(self.current / "request.json")
                except (OSError, ValueError):
                    pass
            write_json(self.root / "worker.json", dict(self.identity, state=self.state,
                       revision=current.get("revision"), label=current.get("label"), pulse=bool(current.get("pulse")),
                       job=self.current.name if self.current else None, updated_at=time.time()))
            self.last_beat = time.monotonic()

    def flags(self):
        now = (self.lock / "STOP_NOW").exists()
        drain = (self.lock / "DRAIN").exists()
        return now, drain

    def on_signal(self, *_):
        self.interrupted = True

    def expired(self):
        return self.args.max_runtime and time.monotonic() - self.born >= self.args.max_runtime

    def pending(self, foreground_only=False):
        pending = []
        for job in jobs(self.root):
            state = read_json(job / "status.json")["state"]
            if state != "PENDING":
                continue
            if (job / "CANCEL").exists():
                set_state(job, "CANCELLED", detail="执行前取消")
                continue
            try:
                spec = read_json(job / "request.json")
                submitted = float(spec["submitted_at"])
                if not math.isfinite(submitted):
                    raise ValueError("invalid submitted_at")
                background = bool(spec.get("background", False))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                set_state(job, "FAILED", detail="坏请求：" + str(exc), finished_at=time.time())
                continue
            if not foreground_only or not background:
                pending.append((background, submitted, job.name, job))
        return sorted(pending)

    def finish_guard(self):
        if self.proc is None:
            return None
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=self.args.grace + 8)
        except subprocess.TimeoutExpired:
            # Do not kill the only remaining supervisor; fail closed for inspection.
            raise RuntimeError("守护进程清理超时；保留锁，不启动下一任务")
        self.proc = None
        result = read_json(self.current / ("guard-" + self.guard_token + ".json"))
        if result.get("cleaned") is not True:
            raise RuntimeError("缺少清理完成证明；保留锁，不启动下一任务")
        return result

    def execute(self, job):
        previous = read_json(job / "status.json")
        attempt = int(previous.get("attempt", 0)) + 1
        preemptions = int(previous.get("preemptions", 0))
        self.current, self.state = job, "RUNNING"
        self.heartbeat(True)
        started = time.time()
        set_state(job, "RUNNING", started_at=started, worker=self.identity,
                  attempt=attempt, preemptions=preemptions)
        outcome, detail, returncode = "FAILED", "", None
        guard_result, spec = None, {}
        try:
            spec = read_json(job / "request.json")
            limit = float(spec["timeout"])
            if not math.isfinite(limit) or limit <= 0:
                raise ValueError("timeout 必须是有限正数")
            validate_payload(job, spec)
            if not spec.get('pulse'):
                # Fast tasks can start between throttled queue scans. Record an
                # exact receipt before launching, even when the scan missed them.
                write_json(job/'received.json', dict(state='READ', revision=spec.get('revision'),
                           worker_token=self.identity['token'], received_at=time.time()))
            now, drain = self.flags()
            if self.interrupted or self.expired() or now or (job / "CANCEL").exists():
                outcome = "INTERRUPTED" if self.interrupted or self.expired() else "CANCELLED"
            elif drain:
                set_state(job, "PENDING", attempt=attempt-1, preemptions=preemptions)
                self.current = None
                return
            else:
                (job / 'results').mkdir(exist_ok=True)
                env = dict(os.environ, PYTHONUNBUFFERED="1", GPUQ_JOB_ID=job.name,
                           GPUQ_JOB_DIR=str(job), GPUQ_ATTEMPT=str(attempt),
                           GPUQ_BACKGROUND="1" if spec.get("background") else "0",
                           GPUQ_CODE_DIR=str(job / "code") if spec.get("source") is not None else spec["cwd"],
                           PYTHONDONTWRITEBYTECODE="1", NICHY_PYTHON=sys.executable,
                           NICHY_APP=str(Path(__file__).resolve().parent),
                           NICHY_JOB_ID=job.name, NICHY_REVISION=spec.get('revision', ''),
                           NICHY_OUTPUT_DIR=str(job / 'results'))
                env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
                if spec.get("pulse"):
                    env["CUDA_VISIBLE_DEVICES"] = spec["pulse_gpu_uuid"]
                    env['PYTHONPATH'] = str(Path(__file__).resolve().parent) + os.pathsep + env.get('PYTHONPATH', '')
                self.guard_token = uuid.uuid4().hex
                local_limit = min(limit, max(.001, self.args.max_runtime - (time.monotonic() - self.born))) if self.args.max_runtime else limit
                with (job / "run.log").open("ab", buffering=0) as log:
                    self.proc = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "_supervise", str(job),
                         spec["cwd"], str(self.args.grace), self.guard_token, str(local_limit)],
                        env=env, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    deadline = time.monotonic() + limit
                    while True:
                        returncode = self.proc.poll()
                        if returncode is not None:
                            outcome = "SUCCEEDED" if returncode == 0 else "FAILED"
                            break
                        self.heartbeat()
                        now, drain = self.flags()
                        if self.interrupted or self.expired():
                            outcome = "INTERRUPTED"
                            break
                        if now or (job / "CANCEL").exists():
                            outcome = "CANCELLED"
                            break
                        if time.monotonic() >= deadline:
                            outcome = "TIMED_OUT"
                            break
                        if spec.get("background") and (drain or self.pending(foreground_only=not spec.get("pulse")) or self.extra_preempt(spec)):
                            outcome = "PREEMPTED"
                            break
                        time.sleep(self.args.poll)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            detail = str(exc)
        finally:
            if self.proc is not None:
                guard_result = self.finish_guard()
                returncode = guard_result["returncode"]
                if guard_result.get("error"):
                    outcome, detail = "FAILED", guard_result["error"]
                elif guard_result.get("timed_out"):
                    outcome = "INTERRUPTED" if self.expired() else "TIMED_OUT"
        if self.current is not None:
            extras = dict(started_at=started, finished_at=time.time(), returncode=returncode,
                          detail=detail, attempt=attempt, preemptions=preemptions,
                          revision=spec.get("revision"), label=spec.get("label"))
            if outcome == "PREEMPTED":
                extras["preemptions"] += 1
                if spec.get("resume_safe") and extras["preemptions"] <= spec.get("max_preemptions", 10):
                    outcome = "PENDING"
                    extras["detail"] = "后台任务已清理，等待空闲后从自身 checkpoint 恢复"
            set_state(job, outcome, **extras)
            write_json(job / ("attempt-%04d.json" % attempt), dict(state=outcome, **extras))
        self.current, self.state = None, "IDLE"
        self.heartbeat(True)

    def run(self):
        become_subreaper()
        try:
            self.lock.mkdir(mode=0o700)
        except FileExistsError:
            raise ValueError("已有 worker 或遗留锁。先查看 status；不要直接删除锁。")
        completed = False
        try:
            write_json(self.lock / "owner.json", self.identity)
            sync_dir(self.lock)
            sync_dir(self.root)
            signal.signal(signal.SIGTERM, self.on_signal)
            signal.signal(signal.SIGINT, self.on_signal)
            for job in jobs(self.root):
                if read_json(job / "status.json")["state"] == "RUNNING":
                    raise ValueError("发现未确认的 RUNNING 任务；请确认旧作业终止后使用 recover。")
            print("worker ready: " + str(self.root), flush=True)
            self.state = "IDLE"
            idle_since = time.monotonic()
            while True:
                self.heartbeat()
                now, drain = self.flags()
                if self.interrupted or self.expired() or now or drain:
                    break
                pending = self.pending()
                if pending and (not pending[0][0] or time.monotonic() - idle_since >= self.args.background_after):
                    self.execute(pending[0][3])
                    idle_since = time.monotonic()
                    continue
                if not pending:
                    optional = self.idle_job(idle_since)
                    if optional is not None:
                        self.execute(optional)
                        continue
                if self.args.idle_exit and time.monotonic() - idle_since >= self.args.idle_exit:
                    break
                time.sleep(self.args.poll)
            self.state = "STOPPED"
            self.heartbeat(True)
            completed = True
        finally:
            try:
                if self.proc is not None:
                    self.finish_guard()
            finally:
                if not completed:
                    self.state = "BLOCKED"
                    try:
                        self.heartbeat(True)
                    except OSError:
                        pass
                else:
                    # Successful shutdown only. Crash locks require explicit recovery.
                    shutil.rmtree(self.lock)
                    sync_dir(self.root)


def status(root, args):
    path = root / "worker.json"
    if args.json:
        info = read_json(path) if path.exists() else None
        selected = [job_path(root, args.id)] if args.id else jobs(root)
        print(json.dumps(dict(worker=info, jobs={j.name: read_json(j / "status.json") for j in selected}), ensure_ascii=False))
        return
    if path.exists():
        info = read_json(path)
        age = max(0, time.time() - info["updated_at"])
        label = "（心跳过期，仅供排查）" if age > 30 and info["state"] not in {"STOPPED", "RECOVERED"} else ""
        print("worker: {} | heartbeat {:.0f}s ago {}".format(info["state"], age, label))
    else:
        print("worker: 尚未启动")
    selected = [job_path(root, args.id)] if args.id else jobs(root)
    for job in selected:
        data = read_json(job / "status.json")
        print(job.name + "\t" + data["state"])
        if args.id:
            print(json.dumps(data, ensure_ascii=False, indent=2))


def recover(root):
    # Caller explicitly asserts the old allocation AND all its tasks are stopped.
    lock = root / "worker.lock"
    owner_path = lock / "owner.json"
    if owner_path.exists():
        owner = read_json(owner_path)
        if owner.get("host") == socket.gethostname() and owner.get("process_stamp") is not None and process_stamp(owner["pid"]) == owner["process_stamp"]:
            raise ValueError("旧 worker 仍在本机运行，拒绝 recover")
    if not lock.exists():
        lock.mkdir(mode=0o700)
    (lock / "RECOVERING").mkdir(mode=0o700)  # Concurrent recovery fails closed.
    for job in jobs(root):
        if read_json(job / "status.json")["state"] == "RUNNING":
            set_state(job, "UNKNOWN", detail="旧作业已确认终止；检查结果后用新 ID 显式恢复，不自动重跑。")
    write_json(root / "worker.json", dict(state="RECOVERED", updated_at=time.time()))
    shutil.rmtree(lock)
    sync_dir(root)
    print("恢复完成；UNKNOWN 不会重跑，PENDING 会在下次 worker 启动后继续。")


def number(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("必须是有限非负数")
    return result


def main():
    if len(sys.argv) == 7 and sys.argv[1] == "_supervise":
        sys.exit(supervise(Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]), sys.argv[5], float(sys.argv[6])))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent / ".nichy"), help="CPU/GPU 必须使用同一个共享路径")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("worker", help="在已申请的 GPU 作业里前台运行")
    p.add_argument("--idle-exit", type=number, default=900, help="空闲秒数；0 表示持续等待，需平台允许")
    p.add_argument("--poll", type=number, default=1)
    p.add_argument("--grace", type=number, default=5)
    p.add_argument("--background-after", type=number, default=30, help="持续空闲多久后允许后台任务")
    p.add_argument("--max-runtime", type=number, default=0, help="worker 最长秒数，到期中断并停止；0 不限制")
    p = sub.add_parser("submit", help="CPU 侧发布 bash 脚本，输出任务 ID")
    p.add_argument("script")
    p.add_argument("--label", help="显示名称")
    p.add_argument("--id", help="CPU 任务重试时使用固定 ID 防重复")
    p.add_argument("--cwd", default=os.getcwd(), help="GPU 上执行脚本的目录")
    p.add_argument("--timeout", type=number, default=86400, help="任务最长秒数")
    p.add_argument("--source", help="可选的小型代码目录快照；通过 GPUQ_CODE_DIR 引用")
    p.add_argument("--snapshot-limit-mib", type=number, default=100)
    p.add_argument("--background", action="store_true", help="可被正式任务抢占的有限后台工作")
    p.add_argument("--resume-safe", action="store_true", help="确认脚本幂等或支持断点恢复，抢占后允许再次执行")
    p.add_argument("--max-preemptions", type=int, default=10, help="允许抢占后重试的最大次数")
    p = sub.add_parser("status", help="查看 worker 和任务状态")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("wait", help="等待任务终态；成功退出 0、任务失败退出 1、等待超时退出 2")
    p.add_argument("id")
    p.add_argument("--deadline", type=number, default=3600)
    p = sub.add_parser("logs", help="打印最后若干行日志")
    p.add_argument("id")
    p.add_argument("--lines", type=int, default=100)
    p = sub.add_parser("cancel", help="请求取消一个任务")
    p.add_argument("id")
    p = sub.add_parser("stop", help="默认当前任务完成后停止 worker")
    p.add_argument("--now", action="store_true", help="取消当前任务并停止 worker")
    p = sub.add_parser("recover", help="只在平台确认旧作业及其子进程全部停止后使用")
    p.add_argument("--confirm-old-worker-stopped", action="store_true", required=True)
    args = parser.parse_args()
    if getattr(args, "resume_safe", False) and not args.background:
        parser.error("--resume-safe 只能用于 --background")
    if getattr(args, "max_preemptions", 0) < 0:
        parser.error("--max-preemptions 不可为负")
    if getattr(args, "poll", 1) <= 0 or getattr(args, "timeout", 1) <= 0 or getattr(args, "lines", 1) <= 0:
        parser.error("poll、timeout、lines 必须大于 0")
    os.umask(0o077)
    root = Path(os.path.abspath(args.root))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("jobs", "staging"):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    if args.command == "worker":
        Worker(root, args).run()
    elif args.command == "submit":
        submit(root, args)
    elif args.command == "status":
        status(root, args)
    elif args.command == "wait":
        job = job_path(root, args.id)
        deadline = time.monotonic() + args.deadline
        while True:
            data = read_json(job / "status.json")
            if data["state"] in TERMINAL:
                print(json.dumps(data, ensure_ascii=False))
                sys.exit(0 if data["state"] == "SUCCEEDED" else 1)
            if time.monotonic() >= deadline:
                print("等待超时；任务未被取消", file=sys.stderr)
                sys.exit(2)
            time.sleep(.2)
    elif args.command == "logs":
        log = job_path(root, args.id) / "run.log"
        if log.exists():
            with log.open(errors="replace") as stream:
                print("".join(collections.deque(stream, maxlen=args.lines)), end="")
        else:
            print("尚无日志；请用 status 查看是否已运行。")
    elif args.command == "cancel":
        job = job_path(root, args.id)
        if read_json(job / "status.json")["state"] in TERMINAL:
            print("任务已经结束；无需取消。")
        else:
            atomic_write(job / "CANCEL", b"cancel\n")
            print("取消请求已提交；以最终 status 为准。")
    elif args.command == "stop":
        # Open the CURRENT lock directory, so a restart cannot receive an old stop.
        fd = os.open(str(root / "worker.lock"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            marker = "STOP_NOW" if args.now else "DRAIN"
            control = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
            os.close(control)
            os.fsync(fd)
        finally:
            os.close(fd)
        print("停止请求已提交；等待 worker 状态变为 STOPPED。")
    elif args.command == "recover":
        recover(root)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        sys.exit(1)
