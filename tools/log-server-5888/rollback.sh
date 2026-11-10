#!/usr/bin/env bash
# 回滚 5888 /upload_progress 补丁：从最近一次 apply.sh 的备份恢复。
#
# 用法：APP=/acli/app.py bash /tmp/rollback.sh
# 注意：回滚只改磁盘代码，必须再让 gunicorn worker 生效（见 README.md）。
set -euo pipefail

APP="${APP:-/acli/app.py}"
PY="${PY:-/acli/.venv/bin/python}"
BASELINE_SHA256="2e3c19718849aa21a4eb0c623c52335ed538bf2023c514ef5ac00d2cc37ce231"
TRACE="rollback-5888-$(date +%Y%m%d%H%M%S)-$$"

log() { echo "[trace=${TRACE}] $*"; }

BACKUP="$(ls -1t "${APP}".bak.upload_progress.* 2>/dev/null | head -1 || true)"
[ -n "$BACKUP" ] && [ -f "$BACKUP" ] || { log "拒绝：找不到备份文件"; exit 1; }

if [ "$(sha256sum "$APP" | awk '{print $1}')" = "$BASELINE_SHA256" ]; then
  log "app.py 已是基线，无需回滚"
  exit 0
fi

cp -p "$BACKUP" "$APP"
"$PY" -m py_compile "$APP" || { log "恢复后语法检查失败，请人工介入"; exit 1; }

ACTUAL_SHA="$(sha256sum "$APP" | awk '{print $1}')"
[ "$ACTUAL_SHA" = "$BASELINE_SHA256" ] || { log "警告：恢复后 sha256=${ACTUAL_SHA} 与基线不符"; exit 1; }

log "回滚完成 backup=${BACKUP} sha256=${ACTUAL_SHA}"
log "请让 gunicorn worker 重新加载，否则内存里仍是补丁后的代码"

