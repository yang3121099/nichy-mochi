"""Optional helper for an existing Linux Supervisor installation."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from nichy import queue_root
from mochi_core import atomic_write


def main():
    parser = argparse.ArgumentParser(description='安装 NichyMochi 的 Supervisor 服务')
    parser.add_argument('--home', help='所选的共享队列位置；默认读取 NICHY_HOME')
    args = parser.parse_args()
    ctl = shutil.which('supervisorctl')
    conf_dir = Path('/etc/supervisor/conf.d')
    if not sys.platform.startswith('linux') or not ctl or not conf_dir.is_dir():
        raise ValueError('这里没有适用的 Supervisor；请把 nichy serve 作为平台作业入口。')
    root = queue_root(args.home)
    scripts = Path('/opt/supervisor-scripts')
    if not scripts.is_dir():
        scripts = root / 'service'
        scripts.mkdir(exist_ok=True)
    wrapper = scripts / 'nichy-mochi.sh'
    conf = conf_dir / 'nichy-mochi.conf'
    if conf.exists() or wrapper.exists():
        raise ValueError('服务文件已存在，请先检查现有服务；不会覆盖。')
    # Generated from the selected paths and Python environment.
    body = '#!/bin/bash\nset -e\n'
    utils = scripts / 'utils'
    for name in ['logging.sh', 'environment.sh']:
        if (utils/name).is_file():
            body += '. '+shlex.quote(str(utils/name))+'\n'
    if 'CUDA_VISIBLE_DEVICES' in os.environ:
        body += 'export CUDA_VISIBLE_DEVICES='+shlex.quote(os.environ['CUDA_VISIBLE_DEVICES'])+'\n'
    body += 'export NICHY_HOME='+shlex.quote(str(root))+'\n'
    body += 'export NICHY_PYTHON='+shlex.quote(sys.executable)+'\n'
    body += 'exec '+shlex.join(['bash',str(APP/'start.sh')])+'\n'
    wrapper_arg = shlex.quote(str(wrapper)).replace('%','%%')
    if '\n' in wrapper_arg or ';' in wrapper_arg:
        raise ValueError('Supervisor 脚本位置不能包含换行或分号。')
    configuration = f'''[program:nichy-mochi]
environment=PROC_NAME="%(program_name)s"
command=/bin/bash {wrapper_arg}
autostart=true
autorestart=false
startretries=0
startsecs=3
stopsignal=TERM
stopwaitsecs=15
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
stdout_logfile_backups=0
'''
    atomic_write(wrapper,body.encode());wrapper.chmod(0o700)
    atomic_write(conf,configuration.encode());conf.chmod(0o600)
    subprocess.run([ctl,'reread'],check=True)
    subprocess.run([ctl,'update','nichy-mochi'],check=True)
    print('Mochi 的服务已安装；共享目录：'+str(root))


if __name__ == '__main__':
    try:
        main()
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print('Mochi 没能安装服务：'+str(exc),file=sys.stderr)
        sys.exit(1)
