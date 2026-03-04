#!/usr/bin/env bash
# ============================================================================
# Agent 质量门禁脚本
# 用途: VK Workspace Cleanup Script 自动触发，或手动运行 make quality-gate
# 功能: 根据变更文件类型自动运行对应的 lint + test 检查
# ============================================================================

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # 无颜色

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# 计数器
PASS=0
FAIL=0
SKIP=0

# 加载 VK 工作流钩子（如果存在）
VK_HOOKS="${PROJECT_ROOT}/scripts/vk-hooks.sh"
if [ -f "$VK_HOOKS" ]; then
    source "$VK_HOOKS"
fi

# 报告文件
REPORT_FILE="${PROJECT_ROOT}/.vk/reports/quality-gate-$(date +%Y%m%d-%H%M%S).txt"
mkdir -p "$(dirname "$REPORT_FILE")"

# ---- 工具函数 ----

log_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
}

log_pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    PASS=$((PASS + 1))
}

log_fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    FAIL=$((FAIL + 1))
}

log_skip() {
    echo -e "  ${YELLOW}⊘ SKIP${NC}: $1"
    SKIP=$((SKIP + 1))
}

# ---- 检测变更文件类型 ----

# 获取当前分支相对于 base 分支的变更文件
# 如果在 worktree 中，对比 base branch；否则对比 HEAD
detect_changes() {
    local base_branch="${VK_BASE_BRANCH:-main}"

    if git merge-base --is-ancestor "$base_branch" HEAD 2>/dev/null; then
        CHANGED_FILES=$(git diff --name-only "$base_branch"...HEAD 2>/dev/null || echo "")
    else
        # 回退：对比工作区变更
        CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null || echo "")
    fi

    # 如果没有检测到变更，检查未提交的变更
    if [ -z "$CHANGED_FILES" ]; then
        CHANGED_FILES=$(git diff --name-only 2>/dev/null || echo "")
        CHANGED_FILES+=$'\n'
        CHANGED_FILES+=$(git diff --name-only --cached 2>/dev/null || echo "")
    fi

    HAS_PYTHON=false
    HAS_FRONTEND=false

    while IFS= read -r file; do
        [[ -z "$file" ]] && continue
        if [[ "$file" == *.py ]]; then
            HAS_PYTHON=true
        fi
        if [[ "$file" == frontend/* ]] || [[ "$file" == *.ts ]] || [[ "$file" == *.vue ]] || [[ "$file" == *.tsx ]]; then
            HAS_FRONTEND=true
        fi
    done <<< "$CHANGED_FILES"
}

# ---- Python 检查 ----

check_python_lint() {
    log_header "Python 静态检查 (ruff check)"

    if ! command -v ruff &>/dev/null && ! uv run ruff --version &>/dev/null 2>&1; then
        log_skip "ruff 未安装"
        return
    fi

    if uv run ruff check backend/ tests/ 2>/dev/null; then
        log_pass "ruff check — 无 error"
    else
        log_fail "ruff check — 发现问题，请运行 'uv run ruff check --fix' 修复"
    fi
}

check_python_format() {
    log_header "Python 格式检查 (ruff format)"

    if uv run ruff format --check backend/ tests/ 2>/dev/null; then
        log_pass "ruff format — 格式一致"
    else
        log_fail "ruff format — 格式不一致，请运行 'uv run ruff format' 修复"
    fi
}

check_python_test() {
    log_header "Python 单元测试 (pytest)"

    local test_pass=true

    # 根级测试（默认排除集成测试，集成测试需要数据库等外部依赖）
    local pytest_ignore_integration="--ignore=tests/integration"
    if [ -d "tests/" ]; then
        if uv run pytest tests/ $pytest_ignore_integration -q --tb=short 2>/dev/null; then
            log_pass "tests/ — 单元测试通过"
        else
            log_fail "tests/ — 部分失败"
            test_pass=false
        fi
    fi

    # 按服务隔离运行（避免 app/ 命名空间冲突，跳过集成测试）
    for service_dir in backend/*/; do
        local test_dir="${service_dir}tests/"
        if [ -d "$test_dir" ]; then
            local ignore_flag=""
            [ -d "${test_dir}integration" ] && ignore_flag="--ignore=${test_dir}integration"
            if uv run pytest "$test_dir" $ignore_flag -q --tb=short 2>/dev/null; then
                log_pass "${test_dir} — 单元测试通过"
            else
                log_fail "${test_dir} — 部分失败"
                test_pass=false
            fi
        fi
    done

    if $test_pass; then
        log_pass "Python 测试全部通过"
    fi
}

# ---- 前端检查 ----

check_frontend_lint() {
    log_header "前端 Lint 检查"

    # 检查是否真正安装了前端依赖（仅有 .pnpm-workspace-state 不算）
    if [ ! -d "frontend/node_modules/.pnpm" ]; then
        log_skip "前端依赖未安装（运行 'cd frontend && pnpm install'）"
        return
    fi

    local lint_pass=true

    for app_dir in frontend/customer frontend/admin; do
        if [ -f "${app_dir}/package.json" ]; then
            # 检查 package.json 是否定义了 lint 脚本
            if ! grep -q '"lint"' "${app_dir}/package.json" 2>/dev/null; then
                log_skip "${app_dir} lint — 未定义 lint 脚本"
                continue
            fi
            if (cd "$app_dir" && pnpm lint 2>/dev/null); then
                log_pass "${app_dir} lint — 通过"
            else
                log_fail "${app_dir} lint — 发现问题"
                lint_pass=false
            fi
        fi
    done

    if $lint_pass; then
        log_pass "前端 lint 全部通过"
    fi
}

# ---- 主流程 ----

main() {
    log_header "Agent 质量门禁 — $(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "  项目: ${PROJECT_ROOT}"
    echo -e "  分支: $(git branch --show-current 2>/dev/null || echo 'unknown')"

    # 检测变更类型
    detect_changes

    echo -e "\n  变更检测:"
    echo -e "    Python 变更: $([ "$HAS_PYTHON" = true ] && echo '是' || echo '否')"
    echo -e "    前端变更:    $([ "$HAS_FRONTEND" = true ] && echo '是' || echo '否')"

    # 如果没有检测到任何变更，运行全部检查
    if [ "$HAS_PYTHON" = false ] && [ "$HAS_FRONTEND" = false ]; then
        echo -e "\n  ${YELLOW}未检测到特定变更，运行全部检查${NC}"
        HAS_PYTHON=true
        HAS_FRONTEND=true
    fi

    # 按变更类型运行检查
    if [ "$HAS_PYTHON" = true ]; then
        check_python_lint
        check_python_format
        check_python_test
    fi

    if [ "$HAS_FRONTEND" = true ]; then
        check_frontend_lint
    fi

    # 输出汇总
    log_header "质量门禁汇总"
    echo -e "  ${GREEN}通过: ${PASS}${NC}"
    echo -e "  ${RED}失败: ${FAIL}${NC}"
    echo -e "  ${YELLOW}跳过: ${SKIP}${NC}"

    # 写入报告文件
    {
        echo "质量门禁报告 — $(date '+%Y-%m-%d %H:%M:%S')"
        echo "分支: $(git branch --show-current 2>/dev/null || echo 'unknown')"
        echo "通过: ${PASS}, 失败: ${FAIL}, 跳过: ${SKIP}"
        echo "结果: $([ $FAIL -eq 0 ] && echo 'PASSED' || echo 'FAILED')"
    } > "$REPORT_FILE"

    echo -e "\n  报告已保存: ${REPORT_FILE}"

    if [ $FAIL -gt 0 ]; then
        echo -e "\n  ${RED}质量门禁未通过！请修复上述问题后重试。${NC}"
        # 触发 VK 失败钩子
        if type vk_on_cleanup_failure &>/dev/null; then
            vk_on_cleanup_failure
        fi
        exit 1
    else
        echo -e "\n  ${GREEN}质量门禁全部通过 ✓${NC}"
        # 触发 VK 成功钩子 — 自动将 Issue 状态流转到 "In review"
        if type vk_on_cleanup_success &>/dev/null; then
            vk_on_cleanup_success
        fi
        exit 0
    fi
}

main "$@"
