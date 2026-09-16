#!/usr/bin/env bash
set -euo pipefail

# 老师只需要设置共享位置；也可使用：bash start.sh /你的共享目录/nichy-data
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export NICHY_HOME="${1:-${NICHY_HOME:-$APP_DIR/.nichy}}"
NICHY_PYTHON="${NICHY_PYTHON:-python3}"  # GPU 环境中已安装 PyTorch 的 Python

# 只在第一次启动时生成配置，不覆盖已有文件。修改后下次启动生效。
"$NICHY_PYTHON" - "$NICHY_HOME" "$APP_DIR" <<'PY'
import os, sys, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from nichy import queue_root
from mochi_core import write_json, sync_dir
root = queue_root(sys.argv[1])
config = root / 'config.json'
if not config.exists():
    temporary = root / ('.config-' + uuid.uuid4().hex)
    try:
        write_json(temporary, {
            'enabled': True,
            'interval': 1800,
            'seconds': 10,
            'idle_for': 30,
            'duty': 0.25
        })
        try:
            os.link(temporary, config)
            sync_dir(root)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
PY

exec "$NICHY_PYTHON" "$APP_DIR/nichy.py" --home "$NICHY_HOME" serve
