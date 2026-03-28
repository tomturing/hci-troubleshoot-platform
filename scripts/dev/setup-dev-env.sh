#!/usr/bin/env bash
# =============================================================================
# setup-dev-env.sh — 开发者本地环境一次性初始化
# 功能：建立 ~/.claude/pitfalls → docs/pitfalls/ 的 symlink，
#       让 Claude Code 等工具在本机自动加载项目避坑指南。
# 用法：bash scripts/dev/setup-dev-env.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
PITFALLS_SRC="$REPO_ROOT/docs/pitfalls"
TARGET="$HOME/.claude/pitfalls"

echo "=== HCI 开发环境初始化 ==="
echo "仓库根目录: $REPO_ROOT"
echo "避坑指南来源: $PITFALLS_SRC"
echo "目标 symlink: $TARGET"
echo ""

# 验证来源目录存在
if [ ! -d "$PITFALLS_SRC" ]; then
  echo "❌ 错误：docs/pitfalls/ 目录不存在，请确认仓库完整性"
  exit 1
fi

# 验证来源目录包含 _index.md
if [ ! -f "$PITFALLS_SRC/_index.md" ]; then
  echo "❌ 错误：docs/pitfalls/_index.md 不存在，来源目录可能不完整"
  exit 1
fi

# 建立 symlink
if [ -L "$TARGET" ]; then
  CURRENT_LINK="$(readlink "$TARGET")"
  if [ "$CURRENT_LINK" = "$PITFALLS_SRC" ]; then
    echo "✅ symlink 已正确指向 $PITFALLS_SRC，无需操作"
    exit 0
  fi
  echo "⚡ 更新 symlink: $CURRENT_LINK → $PITFALLS_SRC"
  rm "$TARGET"
  ln -sf "$PITFALLS_SRC" "$TARGET"
  echo "✅ symlink 已更新"
elif [ -d "$TARGET" ]; then
  echo "⚠️  检测到旧版真实目录 $TARGET"
  echo "   将备份到 $TARGET.bak（请人工确认内容已全部迁移到 docs/pitfalls/）"
  mv "$TARGET" "$TARGET.bak"
  ln -sf "$PITFALLS_SRC" "$TARGET"
  echo "✅ symlink 已建立（旧内容备份至 ~/.claude/pitfalls.bak）"
else
  # 确保父目录存在
  mkdir -p "$(dirname "$TARGET")"
  ln -sf "$PITFALLS_SRC" "$TARGET"
  echo "✅ symlink 已建立: $TARGET → $PITFALLS_SRC"
fi

echo ""
echo "验证："
echo "  $(ls -la "$TARGET" | grep pitfalls)"
echo ""
echo "=== 初始化完成 ==="
