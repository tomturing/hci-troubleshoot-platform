#!/bin/bash
set -euo pipefail

# 获取脚本所在目录的绝对路径，并准确定位项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "==> Running Agent Quality Gate..."

# 运行 evaluation/replay_runner.py
cd "${PROJECT_ROOT}"
if ! uv run python evaluation/replay_runner.py; then
    echo "ERROR: Agent Quality Gate failed! Metrics are below thresholds."
    exit 1
fi

echo "SUCCESS: Agent Quality Gate passed."
exit 0
