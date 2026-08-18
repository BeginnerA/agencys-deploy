#!/usr/bin/env bash
# 智能运维平台多服务流水线发布入口脚本（Linux/macOS/WSL 可用）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PIPELINE=""
VERSION=""
PREVIOUS_VERSION=""
ENV=""
SKIP_BUILD=""
SKIP_SNAILJOB=""
CLEAR_SUMMARY=""
SERVERS_FILE=""
BUILD_ONLY=""
PUSH_ONLY=""

usage() {
  cat <<EOF
用法:
  ./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 [选项]

选项:
  --pipeline            流水线名称（必填）
  --version             新版本号（必填）
  --previous-version    回滚用旧版本号（必填）
  --env                 目标环境，例如 prod/test
  --servers-file        默认服务器清单文件（默认 config/servers.yaml）
  --skip-build          跳过 Maven 构建和镜像推送
  --skip-snailjob       跳过 SnailJob 暂停/恢复
  --clear-summary       全部完成后清零 sj_job_summary
  --build-only          仅构建所有服务镜像，不推送、不发布
  --push-only           构建并推送所有服务镜像到服务器，不发布
  -h, --help            显示本帮助
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pipeline) PIPELINE="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --previous-version) PREVIOUS_VERSION="$2"; shift 2 ;;
    --env) ENV="--env $2"; shift 2 ;;
    --servers-file) SERVERS_FILE="--servers-file $2"; shift 2 ;;
    --skip-build) SKIP_BUILD="--skip-build"; shift ;;
    --skip-snailjob) SKIP_SNAILJOB="--skip-snailjob"; shift ;;
    --clear-summary) CLEAR_SUMMARY="--clear-summary"; shift ;;
    --build-only) BUILD_ONLY="--build-only"; shift ;;
    --push-only) PUSH_ONLY="--push-only"; shift ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

if [[ -z "$PIPELINE" || -z "$VERSION" ]]; then
  echo "错误: --pipeline 和 --version 为必填项"
  usage
fi

# 普通发布/回滚模式需要 --previous-version
if [[ -z "$BUILD_ONLY" && -z "$PUSH_ONLY" && -z "$PREVIOUS_VERSION" ]]; then
  echo "错误: --previous-version 为必填项（--build-only 和 --push-only 模式除外）"
  usage
fi

if [[ -n "$BUILD_ONLY" && -n "$PUSH_ONLY" ]]; then
  echo "错误: --build-only 和 --push-only 不能同时使用"
  exit 1
fi

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

python3 "$SCRIPT_DIR/deploy_pipeline.py" \
  --pipeline "$PIPELINE" \
  --version "$VERSION" \
  --previous-version "$PREVIOUS_VERSION" \
  $ENV $SERVERS_FILE $SKIP_BUILD $SKIP_SNAILJOB $CLEAR_SUMMARY $BUILD_ONLY $PUSH_ONLY
