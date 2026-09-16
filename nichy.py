"""NichyMochi: a tiny conversational front door to a shared-folder queue."""
import argparse
import collections
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

import mochi_core as core


def queue_root(value=None):
    chosen = value or os.environ.get('NICHY_HOME')
    if not chosen and os.environ.get('NICHY_CODE_DIR'):
        chosen = Path(os.environ['NICHY_CODE_DIR']) / 'start'
    if not chosen:
        preferred = Path('/users/nichy/code/start')
        if preferred.is_dir():
            chosen = preferred
        else:
            chosen = Path(__file__).resolve().parent / '.nichy'
            print('Mochi 没找到 /users/nichy/code/start，暂用 '+str(chosen)+'。',file=sys.stderr)
    root = Path(chosen).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('jobs', 'staging'):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    return root


def optional_json(path):
    try:
        return core.read_json(path)
    except FileNotFoundError:
        return None


def latest(root):
    candidates = core.jobs(root)
    return max(candidates, key=lambda j: (core.read_json(j / 'request.json')['submitted_at'], j.name)) if candidates else None


def details(job):
    spec = core.read_json(job / 'request.json')
    return spec, core.read_json(job / 'status.json')


def label(spec):
    return '{} · 版本 {}'.format(spec['label'], spec['revision'][:8])


def online(worker):
    return bool(worker and worker.get('state') in {'IDLE', 'RUNNING'} and
                0 <= time.time() - worker.get('updated_at', 0) <= 30)


def receipt_matches(job, spec):
    receipt = optional_json(job / 'received.json')
    return bool(receipt and receipt.get('state') == 'READ' and receipt.get('revision') == spec['revision'])


def describe(job):
    spec, state = details(job)
    name = label(spec)
    text = {
        'SUCCEEDED': 'Mochi 完成了 ✓：',
        'FAILED': 'Mochi 遇到问题了：',
        'CANCELLED': 'Mochi 已停下：',
        'TIMED_OUT': 'Mochi 等到时间上限了：',
        'INTERRUPTED': 'Mochi 的运行中断了：',
        'UNKNOWN': 'Mochi 需要你检查这次运行：',
        'PREEMPTED': 'Mochi 暂停了后台任务：',
    }
    if state['state'] == 'RUNNING':
        if online(optional_json(job.parent.parent / 'worker.json')):
            return 'Mochi 正在运行：' + name
        return 'Mochi 暂时联系不上机器：' + name + ' · 运行结果待确认'
    if state['state'] == 'PENDING':
        if receipt_matches(job, spec):
            return 'Mochi 收到了：' + name + ' · 排队中'
        return 'Mochi 记下了：' + name + ' · 等待机器读取'
    return text.get(state['state'], 'Mochi 的任务状态待检查：') + name


def run_file(root, args):
    file = Path(args.file).expanduser().resolve(strict=True)
    if not file.is_file() or file.suffix not in {'.py', '.sh'}:
        raise ValueError('请给我一个 .py 或 .sh 文件。')
    command_mode = getattr(args, 'command_file', False)
    if command_mode and file.suffix != '.sh':
        raise ValueError('指令文件需要使用 .sh。')
    if not command_mode and (root == file.parent or root in file.parents):
        raise ValueError('请把程序放在队列目录外，再交给我。')
    interpreter = '"$NICHY_PYTHON" -u' if file.suffix == '.py' else 'bash'
    body = 'cd "$GPUQ_CODE_DIR"\nexec {} {}\n'.format(interpreter, shlex.join(['./'+file.name, *args.file_args]))
    with tempfile.TemporaryDirectory(prefix='nichy-submit-') as temp:
        script = Path(temp) / 'run.sh'
        script.write_bytes(file.read_bytes() if command_mode else body.encode())
        cwd = str(Path(os.environ.get('NICHY_CODE_DIR', str(root.parent))).resolve()) if command_mode else str(root)
        request = argparse.Namespace(script=str(script), id=None, cwd=cwd, timeout=args.timeout,
                    source=None if command_mode else str(file.parent), snapshot_limit_mib=100, background=False,
                    resume_safe=False, max_preemptions=0, label=file.name)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            core.submit(root, request)
    job = core.job_path(root, output.getvalue().strip())
    spec, _ = details(job)
    print('Mochi 记下了：' + label(spec), flush=True)
    end = time.monotonic() + 3
    received = False
    last = None
    log_offset = 0
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    while True:
        _, state = details(job)
        if not received and receipt_matches(job, spec):
            print('Mochi 收到了：' + label(spec), flush=True)
            received = True
        if getattr(args, 'follow', False):
            try:
                with (job/'run.log').open('rb') as log:
                    log.seek(log_offset)
                    data = log.read(256*1024)
                    log_offset += len(data)
                print(decoder.decode(data), end='', flush=True)
            except FileNotFoundError:
                pass
        if state['state'] in core.TERMINAL:
            if getattr(args, 'follow', False):
                try:
                    with (job/'run.log').open('rb') as log:
                        log.seek(log_offset)
                        while data := log.read(256*1024):
                            print(decoder.decode(data), end='', flush=True)
                except FileNotFoundError:
                    pass
                print(decoder.decode(b'',final=True),end='',flush=True)
            print(describe(job), flush=True)
            return 0 if state['state'] == 'SUCCEEDED' else 1
        if args.wait and state['state'] == 'RUNNING' and last != 'RUNNING':
            print(describe(job), flush=True)
        last = state['state']
        if not args.wait and (received or time.monotonic() >= end):
            if not received:
                print('机器还没读取，Mochi 会替你留着；输入 nichy 查看进度。')
            return 0
        if args.wait and time.monotonic() >= end + args.wait_limit:
            print('Mochi 还在等，任务会继续保留；输入 nichy 查看进度。')
            return 2
        time.sleep(.1)


def show_status(root, args):
    worker = optional_json(root / 'worker.json')
    newest = latest(root)
    current = None
    if worker and worker.get('job') and not worker.get('pulse'):
        current = core.job_path(root, worker['job'])
    if args.json:
        selected = {p.name: dict(request=details(p)[0], status=details(p)[1],
                                 receipt=optional_json(p / 'received.json'))
                    for p in core.jobs(root)}
        print(json.dumps(dict(worker=worker, online=online(worker), jobs=selected,
                              keep_alive=optional_json(root / 'keep_alive.json')), ensure_ascii=False))
        return
    if not online(worker):
        print('Mochi 暂时联系不上机器；提交的任务会留在这里。')
    elif not current:
        print('Mochi 在等你 ✨')
    if current:
        print(describe(current))
    if newest and newest != current:
        print('最新提交 · ' + describe(newest))
    if not newest:
        print('把文件交给我吧：nichy run hello.py')


def selected_job(root, job_id=None):
    job = core.job_path(root, job_id) if job_id else latest(root)
    if job is None:
        raise ValueError('还没有任务，先试试 nichy run hello.py。')
    return job


def show_log(root, args):
    job = selected_job(root, args.id)
    print(describe(job))
    log = job / 'run.log'
    if log.exists():
        with log.open(errors='replace') as stream:
            print(''.join(collections.deque(stream, maxlen=args.lines)), end='')
    else:
        print('Mochi 还没有运行输出。')
    print('\n任务文件：' + str(job))


def stop_task(root, args):
    job = None
    if args.id:
        job = core.job_path(root, args.id)
    else:
        running = [p for p in core.jobs(root) if details(p)[1]['state'] == 'RUNNING']
        job = running[0] if running else latest(root)
    if job is None:
        print('Mochi 现在没有任务需要停下。')
        return
    spec, state = details(job)
    if state['state'] in core.TERMINAL:
        print('Mochi 已经结束这次任务了：' + label(spec))
    else:
        core.atomic_write(job / 'CANCEL', b'cancel\n')
        print('Mochi 收到停止请求：' + label(spec) + ' · 正在收尾')


def serve(root, args):
    if not sys.platform.startswith('linux'):
        raise ValueError('请在 Linux GPU 机器上启动 Mochi；这里可以提交和查看任务。')
    from mochi_worker import NichyWorker
    from mochi_meeting import prepare
    prepare(root)
    worker = NichyWorker(root, argparse.Namespace(poll=.2, grace=2, max_runtime=0,
                                                idle_exit=0, background_after=30))
    worker.run()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='NichyMochi · 奶茄团子，把文件交给 Mochi。')
    parser.add_argument('--version', action='version', version='NichyMochi ' + core.VERSION)
    parser.add_argument('--home', help='共享接头点，默认 /users/nichy/code/start')
    sub = parser.add_subparsers(dest='command')
    p = sub.add_parser('run', help='把文件交给 Mochi')
    p.add_argument('--wait', action='store_true', help='一直看到运行结束')
    p.add_argument('--follow', action='store_true', help='等待结束并显示实时输出')
    p.add_argument('--command-file', action='store_true', help='运行指令文件；只快照 shell 指令')
    p.add_argument('--wait-limit', type=core.number, default=86400, help=argparse.SUPPRESS)
    p.add_argument('--timeout', type=core.number, default=86400, help='运行时间上限，默认一天')
    p.add_argument('file')
    p.add_argument('file_args', nargs=argparse.REMAINDER, help='传给文件的参数')
    p = sub.add_parser('status', help='看看进度（直接输入 nichy 也可以）')
    p.add_argument('--json', action='store_true', help='完整状态')
    p = sub.add_parser('log', help='看看最新提交的运行输出')
    p.add_argument('id', nargs='?', help='可选：查看某次任务')
    p.add_argument('--lines', type=int, default=100)
    p = sub.add_parser('stop', help='停下当前任务，机器继续待命')
    p.add_argument('id', nargs='?', help='可选：停止某次任务')
    sub.add_parser('serve', help='在 GPU 上启动监听进程')
    sub.add_parser('home', help='显示实际接头点路径')
    args = parser.parse_args(argv)
    if getattr(args,'follow',False):
        args.wait = True
    if getattr(args, 'timeout', 1) <= 0 or getattr(args, 'lines', 1) <= 0:
        parser.error('运行时间和日志行数需要大于 0。')
    os.umask(0o077)
    try:
        root = queue_root(args.home)
        command = args.command or 'status'
        if command == 'home':
            print(root)
            return 0
        if args.command is None:
            args.json = False
        result = {'run': run_file, 'status': show_status, 'log': show_log,
                  'stop': stop_task, 'serve': serve}[command](root, args)
        return result or 0
    except KeyboardInterrupt:
        print('\nMochi 不再等待显示；已提交的任务继续保留。', file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Mochi 没能完成：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
