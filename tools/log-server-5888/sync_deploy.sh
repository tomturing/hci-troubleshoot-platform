#!/usr/bin/env bash
# 将权威源码目录 backend/log-server/ 同步部署到 5888 日志服务器（192.168.0.2:/acli）
#
# 安全门禁（缺一不可）：
#   1. 本地先跑 backend/log-server/tests/unit 单元测试；
#   2. 远端先执行 preflight_restart.py 硬门禁，有在途上传/合片直接中止；
#   3. 同步核心运行文件前自动在远端打时间戳备份，py_compile 失败自动回滚；
#   4. 支持 --dry-run 只做门禁检查与差异预览，不修改远端任何文件。
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-192.168.0.2}"
REMOTE_DIR="${REMOTE_DIR:-/acli}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *)
      echo "未知参数: $arg" >&2
      echo "用法: $0 [--dry-run]" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_DIR="${REPO_ROOT}/backend/log-server"

RUNTIME_FILES=(
  "app.py"
  "archive_index.py"
  "process_upload.py"
  "session_reaper.py"
  "lifecycle_clean.py"
  "gunicorn_config.py"
  "requirements.txt"
  "templates/upload.html"
)

echo "[1/5] 本地运行 log-server 单元测试..."
(cd "${REPO_ROOT}" && uv run pytest backend/log-server/tests/unit/ -n0 -q)

echo "[2/5] 计算待同步文件 SHA256 清单..."
(cd "${SRC_DIR}" && sha256sum "${RUNTIME_FILES[@]}")

echo "[3/5] 远端执行重启前硬门禁（preflight_restart.py）..."
scp -q "${SCRIPT_DIR}/preflight_restart.py" "${REMOTE_HOST}:/tmp/preflight_restart.py"
PREFLIGHT_OUT="$(ssh "${REMOTE_HOST}" "${REMOTE_DIR}/.venv/bin/python /tmp/preflight_restart.py")"
echo "${PREFLIGHT_OUT}"
if ! echo "${PREFLIGHT_OUT}" | grep -q '"allowed": true'; then
  echo "ERROR: 远端存在活跃上传/合片任务，拒绝部署！" >&2
  exit 1
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[DRY-RUN] 门禁通过，未修改远端 ${REMOTE_HOST}:${REMOTE_DIR}。"
  exit 0
fi

BACKUP_TAG="$(date +%Y%m%d%H%M%S)"
echo "[4/5] 远端备份并同步文件（备份标签: ${BACKUP_TAG}）..."
ssh "${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}/.backup/${BACKUP_TAG}/templates"
for rel in "${RUNTIME_FILES[@]}"; do
  ssh "${REMOTE_HOST}" "if [ -f ${REMOTE_DIR}/${rel} ]; then cp -p ${REMOTE_DIR}/${rel} ${REMOTE_DIR}/.backup/${BACKUP_TAG}/${rel}; fi"
  scp -q "${SRC_DIR}/${rel}" "${REMOTE_HOST}:${REMOTE_DIR}/${rel}"
done

echo "[5/5] 远端语法校验（失败自动回滚）..."
if ! ssh "${REMOTE_HOST}" "${REMOTE_DIR}/.venv/bin/python -m py_compile ${REMOTE_DIR}/app.py ${REMOTE_DIR}/archive_index.py ${REMOTE_DIR}/process_upload.py ${REMOTE_DIR}/session_reaper.py ${REMOTE_DIR}/lifecycle_clean.py"; then
  echo "ERROR: 远端语法校验失败，自动回滚到备份 ${BACKUP_TAG}..." >&2
  for rel in "${RUNTIME_FILES[@]}"; do
    ssh "${REMOTE_HOST}" "if [ -f ${REMOTE_DIR}/.backup/${BACKUP_TAG}/${rel} ]; then cp -p ${REMOTE_DIR}/.backup/${BACKUP_TAG}/${rel} ${REMOTE_DIR}/${rel}; fi"
  done
  exit 1
fi

echo "同步完成。请按需执行自然轮换生效："
echo "  scp ${SCRIPT_DIR}/roll_workers.py ${REMOTE_HOST}:/tmp/"
echo "  ssh ${REMOTE_HOST} '${REMOTE_DIR}/.venv/bin/python /tmp/roll_workers.py'"
