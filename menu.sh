#!/usr/bin/env bash
# 智能运维平台交互式发布菜单入口（Linux/macOS/WSL 可用）
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查 Python 依赖
python3 - <<'PY'
try:
    import yaml, requests, pymysql, paramiko
except ImportError as e:
    print(f"缺少依赖: {e}")
    print("请先安装: pip install -r requirements.txt")
    exit(1)
PY

python3 "$SCRIPT_DIR/menu.py"
