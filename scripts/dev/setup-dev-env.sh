#!/usr/bin/env bash
# =============================================================================
# setup-dev-env.sh — 开发者本地环境一次性初始化
# 功能：建立 ~/.claude/pitfalls/ 目录，将各避坑指南文件 symlink 至
#       docs/deploy/pitfalls/ 和 docs/verify/pitfalls/ 中的对应文件
# 用法：bash scripts/dev/setup-dev-env.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
DEPLOY_SRC="$REPO_ROOT/docs/deploy/pitfalls"
VERIFY_SRC="$REPO_ROOT/docs/verify/pitfalls"
TARGET_DIR="$HOME/.claude/pitfalls"

echo "=== HCI 开发环境初始化 ==="
echo "仓库根目录: $REPO_ROOT"
echo "部署类避坑来源: $DEPLOY_SRC"
echo "验证类避坑来源: $VERIFY_SRC"
echo "目标目录: $TARGET_DIR"
echo ""

# 验证来源目录存在
if [ ! -d "$DEPLOY_SRC" ]; then
  echo "❌ 错误：docs/deploy/pitfalls/ 目录不存在，请确认仓库完整性"
  exit 1
fi
if [ ! -d "$VERIFY_SRC" ]; then
  echo "❌ 错误：docs/verify/pitfalls/ 目录不存在，请确认仓库完整性"
  exit 1
fi

# 如果 TARGET_DIR 是旧的 symlink，先转换为真实目录
if [ -L "$TARGET_DIR" ]; then
  echo "⚡ 检测到旧版 symlink $TARGET_DIR，转换为目录..."
  OLD_LINK="$(readlink "$TARGET_DIR")"
  rm "$TARGET_DIR"
  mkdir -p "$TARGET_DIR"
  echo "✅ 已转换为目录（原 symlink 指向: $OLD_LINK）"
elif [ ! -d "$TARGET_DIR" ]; then
  mkdir -p "$TARGET_DIR"
  echo "✅ 目录已创建: $TARGET_DIR"
fi

# 为每个 pitfalls 文件创建/更新 symlink
create_symlinks() {
  local src_dir="$1"
  for file in "$src_dir"/*.md; do
    [ -f "$file" ] || continue
    name="$(basename "$file")"
    target_file="$TARGET_DIR/$name"
    if [ -L "$target_file" ] && [ "$(readlink "$target_file")" = "$file" ]; then
      echo "  ✓ $name（已是最新）"
    else
      ln -sf "$file" "$target_file"
      echo "  ✓ $name → $file"
    fi
  done
}

echo "建立部署类避坑 symlinks..."
create_symlinks "$DEPLOY_SRC"
echo "建立验证类避坑 symlinks..."
create_symlinks "$VERIFY_SRC"

echo ""
echo "验证："
ls -la "$TARGET_DIR" | tail -n +2
echo ""
echo "=== 初始化完成 ==="

