#!/usr/bin/env bash
set -euo pipefail

# WSL quick bootstrap for hci-troubleshoot-platform.
# Usage:
#   bash scripts/wsl_quickstart.sh
#   WITH_DEPS=1 bash scripts/wsl_quickstart.sh
#   PYTHON_VERSION=3.12.3 bash scripts/wsl_quickstart.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="${PYTHON_VERSION:-3.12.3}"
WITH_DEPS="${WITH_DEPS:-0}"
SKIP_DOCKER_CHECK="${SKIP_DOCKER_CHECK:-0}"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
fail() { printf '[ERR ] %s\n' "$*" >&2; exit 1; }

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"
}

is_wsl() {
  grep -qiE "(microsoft|wsl)" /proc/version 2>/dev/null
}

cd "$ROOT_DIR"
info "workspace: $ROOT_DIR"

if is_wsl; then
  info "WSL environment detected"
else
  warn "current shell does not look like WSL; script will continue"
fi

require_cmd git
require_cmd uv
require_cmd bash

if [[ "$SKIP_DOCKER_CHECK" != "1" ]]; then
  require_cmd docker
  if docker info >/dev/null 2>&1; then
    info "docker engine reachable"
  else
    fail "docker engine unreachable; enable Docker Desktop + WSL integration first"
  fi
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  info "created .env from .env.example"
else
  info ".env already exists"
fi

info "ensuring python via uv: $PYTHON_VERSION"
uv python install "$PYTHON_VERSION" >/dev/null
uv venv --python "$PYTHON_VERSION" .venv >/dev/null
info "virtualenv ready: .venv (python $PYTHON_VERSION)"

if [[ "$WITH_DEPS" == "1" ]]; then
  info "installing python deps (WITH_DEPS=1)"
  source .venv/bin/activate
  uv pip install -r tests/requirements.txt
  uv pip install -r backend/api-gateway/requirements.txt
  uv pip install -r backend/case-service/requirements.txt
  uv pip install -r backend/conversation-service/requirements.txt
  uv pip install -r backend/scheduler-service/requirements.txt

  if command -v pnpm >/dev/null 2>&1; then
    info "installing frontend deps"
    pnpm -C frontend install
  else
    warn "pnpm not found; skip frontend deps"
  fi
fi

cat <<'EOF'

Quick start complete.

Next commands:
  source .venv/bin/activate
  docker network create hci-troubleshoot-platform_default 2>/dev/null || true
  docker compose -f deploy/docker/docker-compose.yml up -d --build

For multi-assistant verification:
  bash scripts/test_multi_assistant.sh

Then continue coding in WSL:
  codex

EOF
