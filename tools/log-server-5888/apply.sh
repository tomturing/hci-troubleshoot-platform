#!/usr/bin/env bash
# 幂等落地 5888 /upload_progress 补丁；任何一步失败自动回滚到备份。
#
# 用法（在 192.168.0.2 上，把 app.py.patch 放到 /tmp）：
#   APP=/acli/app.py PATCH=/tmp/app.py.patch bash /tmp/apply.sh
#
# 三道前置门禁：基线 sha256 必须精确匹配、端点不能已存在、patch --dry-run
# 必须通过。基线来自 0f4764e 的现网副本，换基线必须重新生成补丁并更新常量。
set -euo pipefail

APP="${APP:-/acli/app.py}"
PATCH="${PATCH:-/tmp/app.py.patch}"
PY="${PY:-/acli/.venv/bin/python}"
BASELINE_SHA256="2e3c19718849aa21a4eb0c623c52335ed538bf2023c514ef5ac00d2cc37ce231"
APP_DIR="$(dirname "$APP")"
STAMP="$(date +%Y%m%d%H%M%S)"
TRACE="apply-5888-${STAMP}-$$"

log() { echo "[trace=${TRACE}] $*"; }

command -v patch >/dev/null 2>&1 || { log "拒绝：缺少 patch 命令"; exit 1; }
[ -f "$APP" ] || { log "拒绝：$APP 不存在"; exit 1; }
[ -f "$PATCH" ] || { log "拒绝：$PATCH 不存在"; exit 1; }

if grep -q "def upload_progress():" "$APP"; then
  log "端点已存在，幂等退出；如需重打请先执行 rollback.sh"
  exit 0
fi

ACTUAL_SHA="$(sha256sum "$APP" | awk '{print $1}')"
if [ "$ACTUAL_SHA" != "$BASELINE_SHA256" ]; then
  log "拒绝：app.py sha256=${ACTUAL_SHA} 与基线 ${BASELINE_SHA256} 不符，禁止在不明版本上打补丁"
  exit 1
fi

if ! patch --dry-run -p1 -d "$APP_DIR" < "$PATCH"; then
  log "拒绝：patch --dry-run 未通过，未改动任何文件"
  exit 1
fi

BACKUP="${APP}.bak.upload_progress.${STAMP}"
cp -p "$APP" "$BACKUP"
log "已备份 ${BACKUP}"

if ! patch -p1 -d "$APP_DIR" < "$PATCH"; then
  cp -p "$BACKUP" "$APP"
  log "patch 失败，已从备份恢复"
  exit 1
fi

if ! "$PY" -m py_compile "$APP"; then
  cp -p "$BACKUP" "$APP"
  "$PY" -m py_compile "$APP" || true
  log "py_compile 失败，已从备份恢复；现网代码保持基线"
  exit 1
fi

if ! grep -q "@app.route('/upload_progress'" "$APP"; then
  cp -p "$BACKUP" "$APP"
  log "补丁后未找到端点路由，已从备份恢复"
  exit 1
fi

log "完成 baseline=${BASELINE_SHA256} applied=$(sha256sum "$APP" | awk '{print $1}')"
log "patch_sha256=$(sha256sum "$PATCH" | awk '{print $1}') backup=${BACKUP}"
log "下一步：跑 verify_endpoint.py 做隔离验证，再让 worker 生效；未生效前现网仍是旧代码"

