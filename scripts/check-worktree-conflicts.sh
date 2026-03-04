#!/usr/bin/env bash
# ============================================================================
# Worktree 冲突预检脚本
# 用途: 合并前检测不同 Git Worktree 间修改了相同文件的情况
# 运行: make conflict-check 或 bash scripts/check-worktree-conflicts.sh
# ============================================================================

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

log_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
}

# ---- 主流程 ----

main() {
    local base_branch="${1:-main}"
    local conflicts_found=false

    log_header "Worktree 冲突预检 — base: ${base_branch}"

    # 获取所有 worktree 列表
    local worktrees
    worktrees=$(git worktree list --porcelain 2>/dev/null | grep "^worktree " | sed 's/^worktree //')

    local worktree_count
    worktree_count=$(echo "$worktrees" | grep -c . 2>/dev/null || echo "0")

    echo -e "\n  活跃 Worktree 数量: ${worktree_count}"

    if [ "$worktree_count" -le 1 ]; then
        echo -e "  ${GREEN}只有主 worktree，无需冲突检查。${NC}"
        exit 0
    fi

    echo -e "\n  Worktree 列表:"
    while IFS= read -r wt; do
        local branch
        branch=$(git -C "$wt" branch --show-current 2>/dev/null || echo "detached")
        echo -e "    - ${wt} (${branch})"
    done <<< "$worktrees"

    # 收集每个 worktree 相对于 base branch 的变更文件
    declare -A file_worktrees  # file → worktree 列表

    while IFS= read -r wt; do
        local branch
        branch=$(git -C "$wt" branch --show-current 2>/dev/null || echo "detached")

        # 跳过 base branch 本身
        if [ "$branch" = "$base_branch" ]; then
            continue
        fi

        # 获取该 worktree 相对于 base 的变更文件
        local changed_files
        changed_files=$(git -C "$wt" diff --name-only "${base_branch}"..."${branch}" 2>/dev/null || echo "")

        # 加上未提交的变更
        changed_files+=$'\n'
        changed_files+=$(git -C "$wt" diff --name-only 2>/dev/null || echo "")
        changed_files+=$'\n'
        changed_files+=$(git -C "$wt" diff --name-only --cached 2>/dev/null || echo "")

        while IFS= read -r file; do
            [[ -z "$file" ]] && continue
            if [ -n "${file_worktrees[$file]:-}" ]; then
                file_worktrees[$file]="${file_worktrees[$file]}|${branch}"
            else
                file_worktrees[$file]="${branch}"
            fi
        done <<< "$changed_files"
    done <<< "$worktrees"

    # 检测冲突（同一文件被多个 worktree 修改）
    log_header "冲突检测结果"

    local conflict_count=0
    for file in "${!file_worktrees[@]}"; do
        local branches="${file_worktrees[$file]}"
        # 检查是否有多个分支修改了同一文件（含 | 分隔符）
        if [[ "$branches" == *"|"* ]]; then
            conflicts_found=true
            ((conflict_count++))
            echo -e "  ${RED}⚠ 冲突${NC}: ${file}"
            echo -e "    修改分支: ${branches//|/, }"
        fi
    done

    if $conflicts_found; then
        echo -e "\n  ${RED}发现 ${conflict_count} 个潜在冲突！${NC}"
        echo -e "  ${YELLOW}建议：${NC}"
        echo -e "    1. 按依赖顺序逐个合并（shared → backend → frontend）"
        echo -e "    2. 先合并的分支完成后，其他分支 rebase 最新代码"
        echo -e "    3. 如果冲突文件多，考虑将相关任务合并为一个 Workspace"
        exit 1
    else
        echo -e "  ${GREEN}未发现冲突。所有 Worktree 修改的文件互不重叠。${NC}"
        exit 0
    fi
}

main "$@"
