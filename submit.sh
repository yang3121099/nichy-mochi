#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${NICHY_CLIENT_PYTHON:-python3}"
MEETING="$("$PYTHON" "$APP_DIR/nichy.py" home)"

# 无需安装命令。指定文件则快照提交；无参数则提交接头点里的 command.sh。
if [ "$#" -eq 0 ]; then
    exec "$PYTHON" "$APP_DIR/nichy.py" --home "$MEETING" run --follow --command-file "$MEETING/command.sh"
fi
exec "$PYTHON" "$APP_DIR/nichy.py" --home "$MEETING" run --follow "$@"
