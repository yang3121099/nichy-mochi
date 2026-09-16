#!/usr/bin/env bash
set -euo pipefail

# GPU 启动入口；路径可通过参数、NICHY_HOME 或 NICHY_CODE_DIR 覆盖。
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NICHY_PYTHON="${NICHY_PYTHON:-python3}"  # GPU 环境中已安装 PyTorch 的 Python
if [ "$#" -gt 0 ]; then export NICHY_HOME="$1"; fi
NICHY_HOME="$("$NICHY_PYTHON" "$APP_DIR/nichy.py" home)"
export NICHY_HOME

# 只在第一次启动时生成配置，不覆盖已有文件。修改后下次启动生效。
"$NICHY_PYTHON" - "$NICHY_HOME" "$APP_DIR" <<'PY'
import os, sys, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from nichy import queue_root
from mochi_core import write_json, sync_dir
from mochi_meeting import prepare
root = queue_root(sys.argv[1])
prepare(root)
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
