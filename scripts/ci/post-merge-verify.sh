#!/usr/bin/env bash
# ============================================================================
# 合并后集成验证脚本
# 用途: 多个 Workspace 合并到目标分支后，验证整体项目完整性
# 运行: make post-merge 或 bash scripts/ci/post-merge-verify.sh
# ============================================================================

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

PASS=0
FAIL=0
SKIP=0

log_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
}

log_pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    ((++PASS))
}

log_fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    ((++FAIL))
}

log_skip() {
    echo -e "  ${YELLOW}⊘ SKIP${NC}: $1"
    ((++SKIP))
}

# ---- 检查步骤 ----

step_dependencies() {
    log_header "Step 1: 依赖安装验证"

    # Python 依赖
    if [ -f "pyproject.toml" ]; then
        if uv sync --frozen 2>/dev/null; then
            log_pass "Python 依赖安装成功 (uv sync --frozen)"
        else
            log_fail "Python 依赖安装失败"
        fi
    else
        log_skip "无 pyproject.toml"
    fi

    # 前端依赖
    if [ -f "frontend/package.json" ]; then
        local pnpm_cmd=()
        if command -v pnpm >/dev/null 2>&1; then
            pnpm_cmd=(pnpm)
        elif command -v corepack >/dev/null 2>&1; then
            pnpm_cmd=(corepack pnpm)
        else
            log_fail "未找到 pnpm 或 corepack，无法验证前端依赖"
            return
        fi
        if (cd frontend && "${pnpm_cmd[@]}" install --frozen-lockfile 2>/dev/null); then
            log_pass "前端依赖安装成功 (pnpm install)"
        else
            log_fail "前端依赖安装失败或 lockfile 漂移"
        fi
    else
        log_skip "无前端 package.json"
    fi
}

step_lint() {
    log_header "Step 2: 全量 Lint 检查"

    # Python lint
    if uv run ruff check backend/ tests/ 2>/dev/null; then
        log_pass "Python ruff check 通过"
    else
        log_fail "Python ruff check 失败"
    fi

    # 当前 main 尚未建立 Ruff formatter 全仓基线，CI 也只把 ruff check 作为硬门禁。
    # 在基线专项完成前不得让 117 个存量格式差异使每次合并后验证恒定失败。
    log_skip "Python ruff format（待全仓格式基线专项完成后启用）"
}

step_unit_test() {
    log_header "Step 3: 全量单元测试"

    local all_passed=true

    # 根级单元/安全测试。tests/integration 依赖 PostgreSQL 且会同时装载多个
    # 微服务的 app 命名空间，不属于本阶段；跨服务验证由下方 E2E 阶段负责。
    if [ -d "tests/unit" ]; then
        if uv run pytest tests/unit tests/security -q --tb=short 2>/dev/null; then
            log_pass "tests/unit + tests/security 通过"
        else
            log_fail "tests/unit + tests/security 失败"
            all_passed=false
        fi
    fi

    # 按服务隔离运行
    for service_dir in backend/*/; do
        local test_dir="${service_dir}tests/"
        if [ -d "$test_dir" ]; then
            local pytest_args=("$test_dir" -q --tb=short)
            if [ -d "${test_dir}integration" ]; then
                pytest_args+=(--ignore="${test_dir}integration")
            fi
            if uv run pytest "${pytest_args[@]}" 2>/dev/null; then
                log_pass "${test_dir} 通过"
            else
                log_fail "${test_dir} 失败"
                all_passed=false
            fi
        fi
    done
}

step_e2e() {
    log_header "Step 4: E2E 集成测试"

    # 检查 Docker Compose E2E 脚本是否存在
    if [ -x "scripts/docker-e2e-test.sh" ]; then
        echo -e "  ${YELLOW}E2E 测试需要运行中的 Docker 环境${NC}"
        echo -e "  ${YELLOW}跳过自动 E2E，请手动运行: bash scripts/docker-e2e-test.sh${NC}"
        log_skip "E2E 测试（需 Docker 环境，请手动运行）"
    else
        log_skip "E2E 测试脚本不存在或不可执行"
    fi
}

# ---- 主流程 ----

main() {
    log_header "合并后集成验证 — $(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "  项目: ${PROJECT_ROOT}"
    echo -e "  分支: $(git branch --show-current 2>/dev/null || echo 'unknown')"

    step_dependencies
    step_lint
    step_unit_test
    step_e2e

    # 汇总
    log_header "集成验证汇总"
    echo -e "  ${GREEN}通过: ${PASS}${NC}"
    echo -e "  ${RED}失败: ${FAIL}${NC}"
    echo -e "  ${YELLOW}跳过: ${SKIP}${NC}"

    if [ $FAIL -gt 0 ]; then
        echo -e "\n  ${RED}集成验证未通过！请修复问题后重试。${NC}"
        exit 1
    else
        echo -e "\n  ${GREEN}集成验证全部通过 ✓${NC}"
        exit 0
    fi
}

main "$@"
