#!/usr/bin/env bash
# ============================================================================
# Agent 质量门禁脚本 — 自动探测版 v2
#
# 用途: VK Workspace Cleanup Script 自动触发，或手动运行 make quality-gate
# 功能: 自动探测项目结构，零配置适配任意项目
#
# 探测逻辑优先级:
#   Python: pyproject.toml > setup.py > *.py 文件存在
#   Node.js: package.json 存在 + 有 test/lint 脚本
#   Go: go.mod 存在
#   Rust: Cargo.toml 存在
#   源码目录: src/ dispatcher/ backend/ app/ lib/ + 含 __init__.py 的子目录
#   测试目录: tests/ test/ src/tests/ + 任意子服务的 tests/ + **/test_*.py
#   前端: frontend/*/package.json + node_modules/.pnpm/（非空安装标记）
#
# 依赖自检:
#   - Python: uv run ruff 失败时提示 uv sync --dev
#   - Node.js: pnpm/npm test 失败时提示安装依赖
# ============================================================================

set -euo pipefail

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---- 项目根目录 ----
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# ---- 计数器 ----
PASS=0
FAIL=0
SKIP=0

# ---- 加载 VK 工作流钩子 ----
VK_HOOKS="${PROJECT_ROOT}/scripts/vk-hooks.sh"
if [ -f "$VK_HOOKS" ]; then
    source "$VK_HOOKS"
fi

# ---- 报告文件 ----
REPORT_DIR="${PROJECT_ROOT}/.vk/reports"
mkdir -p "$REPORT_DIR"
REPORT_FILE="${REPORT_DIR}/quality-gate-$(date +%Y%m%d-%H%M%S).txt"

# ============================================================================
# 工具函数
# ============================================================================

log_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
}

log_info() { echo -e "  ${CYAN}ℹ${NC} $1"; }
log_pass() { echo -e "  ${GREEN}✓ PASS${NC}: $1"; PASS=$((PASS + 1)); }
log_fail() { echo -e "  ${RED}✗ FAIL${NC}: $1"; FAIL=$((FAIL + 1)); }
log_skip() { echo -e "  ${YELLOW}⊘ SKIP${NC}: $1"; SKIP=$((SKIP + 1)); }

# ============================================================================
# 自动探测项目结构
# ============================================================================

IS_PYTHON=false
IS_FRONTEND=false
IS_NODEJS=false
IS_GO=false
IS_RUST=false
PYTHON_RUNNER=""
PYTHON_SRC_DIRS=()
PYTHON_TEST_DIRS=()
FRONTEND_DIRS=()
NODEJS_DIRS=()
GO_DIRS=()
RUST_DIRS=()

detect_project_structure() {
    log_header "项目结构自动探测"

    # ---- Python 项目检测 ----
    if [ -f "pyproject.toml" ] || [ -f "setup.py" ] || [ -f "setup.cfg" ]; then
        IS_PYTHON=true
    elif find . -maxdepth 2 -name "*.py" \
             -not -path "./.git/*" -not -path "*/.venv/*" \
             -not -path "*/node_modules/*" 2>/dev/null | grep -q .; then
        IS_PYTHON=true
    fi

    if $IS_PYTHON; then
        # 确定运行器
        if command -v uv &>/dev/null && [ -f "pyproject.toml" ]; then
            PYTHON_RUNNER="uv run"
        elif command -v python3 &>/dev/null; then
            PYTHON_RUNNER="python3 -m"
        fi

        # 发现源码目录：先查已知惯用名
        for d in src dispatcher backend app lib core api server; do
            if [ -d "$d" ] && find "$d" -maxdepth 1 -name "*.py" 2>/dev/null | grep -q .; then
                PYTHON_SRC_DIRS+=("$d")
            fi
        done

        # 若未找到，扫描含 __init__.py 的顶层包
        if [ ${#PYTHON_SRC_DIRS[@]} -eq 0 ]; then
            while IFS= read -r pkg; do
                local d="${pkg%/__init__.py}"
                d="${d#./}"
                [[ "$d" == */* ]] && continue   # 只取顶层
                PYTHON_SRC_DIRS+=("$d")
            done < <(find . -maxdepth 2 -name "__init__.py" \
                         -not -path "./.git/*" -not -path "*/.venv/*" \
                         -not -path "*/node_modules/*" 2>/dev/null | head -20)
        fi

        readarray -t PYTHON_SRC_DIRS < <(printf '%s\n' "${PYTHON_SRC_DIRS[@]}" | sort -u)

        # 发现测试目录：优先级 tests/ > test/ > src/tests/ > 子目录 tests/
        for d in tests test; do
            [ -d "$d" ] && PYTHON_TEST_DIRS+=("$d")
        done
        # 支持 src/tests/ 布局
        [ -d "src/tests" ] && PYTHON_TEST_DIRS+=("src/tests")
        while IFS= read -r tdir; do
            local rel="${tdir#./}"
            [[ "$rel" == "tests" || "$rel" == "test" || "$rel" == "src/tests" ]] && continue
            PYTHON_TEST_DIRS+=("$rel")
        done < <(find . -maxdepth 3 -type d -name "tests" \
                     -not -path "./.git/*" -not -path "*/.venv/*" \
                     2>/dev/null | grep -v '^\./tests$' | head -20)

        # 探测 **/test_*.py 文件（无 tests/ 目录的布局）
        if [ ${#PYTHON_TEST_DIRS[@]} -eq 0 ]; then
            local test_files
            test_files=$(find . -name "test_*.py" -not -path "./.git/*" -not -path "*/.venv/*" 2>/dev/null | head -5)
            if [ -n "$test_files" ]; then
                # 有 test_*.py 文件但没有 tests/ 目录，用 pytest 自动发现
                PYTHON_TEST_DIRS+=(".")  # 让 pytest 从根目录发现
                log_info "发现 test_*.py 文件但无 tests/ 目录，使用 pytest 自动发现"
            fi
        fi

        readarray -t PYTHON_TEST_DIRS < <(printf '%s\n' "${PYTHON_TEST_DIRS[@]}" | sort -u)

        log_info "Python: ✓  运行器=${PYTHON_RUNNER:-未找到}"
        log_info "源码目录: ${PYTHON_SRC_DIRS[*]:-（未发现，将检查根目录 *.py）}"
        log_info "测试目录: ${PYTHON_TEST_DIRS[*]:-（未发现）}"
    else
        log_info "Python 项目: 未检测到"
    fi

    # ---- Node.js 项目检测 ----
    # 检查根目录和子目录中的 package.json
    if [ -f "package.json" ]; then
        IS_NODEJS=true
        NODEJS_DIRS+=(".")
    fi
    # 检查子目录（如 packages/、apps/ 等 monorepo 布局）
    while IFS= read -r pkg_json; do
        local dir="${pkg_json%package.json}"
        dir="${dir%\/}"
        [ "$dir" = "." ] && continue
        NODEJS_DIRS+=("$dir")
    done < <(find . -maxdepth 3 -name "package.json" -not -path "*/node_modules/*" 2>/dev/null | head -20)

    if [ ${#NODEJS_DIRS[@]} -gt 0 ]; then
        IS_NODEJS=true
        readarray -t NODEJS_DIRS < <(printf '%s\n' "${NODEJS_DIRS[@]}" | sort -u)
        log_info "Node.js: ✓ 目录=${NODEJS_DIRS[*]}"
    else
        log_info "Node.js 项目: 未检测到"
    fi

    # ---- Go 项目检测 ----
    if [ -f "go.mod" ]; then
        IS_GO=true
        GO_DIRS+=(".")
    fi
    # 检查子模块
    while IFS= read -r go_mod; do
        local dir="${go_mod%go.mod}"
        dir="${dir%\/}"
        [ "$dir" = "." ] && continue
        GO_DIRS+=("$dir")
    done < <(find . -maxdepth 3 -name "go.mod" -not -path "*/vendor/*" 2>/dev/null | head -20)

    if $IS_GO; then
        readarray -t GO_DIRS < <(printf '%s\n' "${GO_DIRS[@]}" | sort -u)
        log_info "Go: ✓ 目录=${GO_DIRS[*]}"
    else
        log_info "Go 项目: 未检测到"
    fi

    # ---- Rust 项目检测 ----
    if [ -f "Cargo.toml" ]; then
        IS_RUST=true
        RUST_DIRS+=(".")
    fi
    # 检查子 crate
    while IFS= read -r cargo_toml; do
        local dir="${cargo_toml%Cargo.toml}"
        dir="${dir%\/}"
        [ "$dir" = "." ] && continue
        RUST_DIRS+=("$dir")
    done < <(find . -maxdepth 3 -name "Cargo.toml" -not -path "*/target/*" 2>/dev/null | head -20)

    if $IS_RUST; then
        readarray -t RUST_DIRS < <(printf '%s\n' "${RUST_DIRS[@]}" | sort -u)
        log_info "Rust: ✓ 目录=${RUST_DIRS[*]}"
    else
        log_info "Rust 项目: 未检测到"
    fi

    # ---- 前端项目检测（Vue/React 特定目录）----
    for app_dir in frontend/customer frontend/admin frontend web ui; do
        if [ -f "${app_dir}/package.json" ] && [ -d "${app_dir}/node_modules/.pnpm" ]; then
            IS_FRONTEND=true
            FRONTEND_DIRS+=("$app_dir")
        fi
    done
    if [ -f "package.json" ] && [ -d "node_modules" ]; then
        IS_FRONTEND=true
        FRONTEND_DIRS+=(".")
    fi

    if $IS_FRONTEND; then
        readarray -t FRONTEND_DIRS < <(printf '%s\n' "${FRONTEND_DIRS[@]}" | sort -u)
        log_info "前端: ✓  目录=${FRONTEND_DIRS[*]}"
    else
        log_info "前端项目: 未检测到（或依赖未安装）"
    fi
}

# ============================================================================
# 变更检测
# ============================================================================

HAS_PYTHON_CHANGES=false
HAS_FRONTEND_CHANGES=false
HAS_NODEJS_CHANGES=false
HAS_GO_CHANGES=false
HAS_RUST_CHANGES=false

detect_changes() {
    local base_branch="${VK_BASE_BRANCH:-main}"
    git rev-parse --verify "$base_branch" &>/dev/null || base_branch="master"

    local changed
    if git merge-base --is-ancestor "$base_branch" HEAD 2>/dev/null; then
        changed=$(git diff --name-only "$base_branch"...HEAD 2>/dev/null || true)
    else
        changed=$(git diff --name-only HEAD 2>/dev/null || true)
    fi
    if [ -z "$changed" ]; then
        changed=$(git diff --name-only 2>/dev/null || true)
        changed+=$'\n'
        changed+=$(git diff --name-only --cached 2>/dev/null || true)
    fi

    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        [[ "$f" == *.py ]] && HAS_PYTHON_CHANGES=true
        [[ "$f" == frontend/* || "$f" == *.ts || "$f" == *.vue || "$f" == *.tsx || "$f" == *.jsx ]] \
            && HAS_FRONTEND_CHANGES=true
        [[ "$f" == *.js || "$f" == *.ts || "$f" == *.tsx || "$f" == *.jsx || "$f" == package.json ]] \
            && HAS_NODEJS_CHANGES=true
        [[ "$f" == *.go || "$f" == go.mod || "$f" == go.sum ]] && HAS_GO_CHANGES=true
        [[ "$f" == *.rs || "$f" == Cargo.toml || "$f" == Cargo.lock ]] && HAS_RUST_CHANGES=true
    done <<< "$changed"
}

# ============================================================================
# Python 检查
# ============================================================================

_ruff_cmd() {
    # 优先级: uv run ruff > uvx ruff > 全局 ruff
    if [ -n "$PYTHON_RUNNER" ] && $PYTHON_RUNNER ruff --version &>/dev/null 2>&1; then
        echo "$PYTHON_RUNNER ruff"
    elif command -v uvx &>/dev/null && uvx ruff --version &>/dev/null 2>&1; then
        echo "uvx ruff"
    elif command -v ruff &>/dev/null; then
        echo "ruff"
    fi
}

# 依赖自检：检测 ruff 是否可用，不可用时给出安装提示
_check_python_deps() {
    local cmd; cmd="$(_ruff_cmd)"
    if [ -n "$cmd" ]; then
        echo "$cmd"
        return 0
    fi

    # ruff 不可用，给出安装提示
    if [ -f "pyproject.toml" ] && command -v uv &>/dev/null; then
        echo -e "  ${YELLOW}⚠ ruff 未安装，请运行: uv sync --dev${NC}" >&2
    elif command -v pip &>/dev/null; then
        echo -e "  ${YELLOW}⚠ ruff 未安装，请运行: pip install ruff${NC}" >&2
    else
        echo -e "  ${YELLOW}⚠ ruff 未安装，请安装 ruff${NC}" >&2
    fi
    return 1
}

check_python_lint() {
    log_header "Python 静态检查 (ruff check)"
    if [ ${#PYTHON_SRC_DIRS[@]} -eq 0 ]; then log_skip "未发现源码目录"; return; fi

    local cmd; cmd="$(_ruff_cmd)"
    if [ -z "$cmd" ]; then
        log_fail "ruff 未安装"
        _check_python_deps  # 显示安装提示
        return
    fi

    local ec=0
    $cmd check "${PYTHON_SRC_DIRS[@]}" 2>&1 || ec=$?
    if [ $ec -eq 0 ]; then
        log_pass "ruff check — 无 error  (${PYTHON_SRC_DIRS[*]})"
    else
        log_fail "ruff check — 有问题 (运行 '$cmd check --fix ${PYTHON_SRC_DIRS[*]}' 修复)"
    fi
}

check_python_format() {
    log_header "Python 格式检查 (ruff format)"
    if [ ${#PYTHON_SRC_DIRS[@]} -eq 0 ]; then log_skip "未发现源码目录"; return; fi

    local cmd; cmd="$(_ruff_cmd)"
    if [ -z "$cmd" ]; then
        log_fail "ruff 未安装"
        _check_python_deps  # 显示安装提示
        return
    fi

    local ec=0
    $cmd format --check "${PYTHON_SRC_DIRS[@]}" 2>&1 || ec=$?
    if [ $ec -eq 0 ]; then
        log_pass "ruff format — 格式一致"
    else
        log_fail "ruff format — 不一致 (运行 '$cmd format ${PYTHON_SRC_DIRS[*]}' 修复)"
    fi
}

check_python_test() {
    log_header "Python 单元测试 (pytest)"
    if [ ${#PYTHON_TEST_DIRS[@]} -eq 0 ]; then
        log_skip "未发现测试目录 (tests/ / test/ / src/tests/)"; return
    fi

    local pytest_cmd
    if [ -n "$PYTHON_RUNNER" ] && $PYTHON_RUNNER pytest --version &>/dev/null 2>&1; then
        pytest_cmd="$PYTHON_RUNNER pytest"
    elif command -v pytest &>/dev/null; then
        pytest_cmd="pytest"
    else
        log_fail "pytest 未安装"
        if [ -f "pyproject.toml" ] && command -v uv &>/dev/null; then
            echo -e "  ${YELLOW}⚠ 请运行: uv sync --dev${NC}"
        fi
        return
    fi

    local all_pass=true
    for tdir in "${PYTHON_TEST_DIRS[@]}"; do
        [[ -z "$tdir" ]] && continue   # 过滤空元素（readarray 可能产生）
        local ignore_flags=()
        [ -d "${tdir}/integration" ] && ignore_flags+=("--ignore=${tdir}/integration")
        # exit code 5 = no tests collected，不算失败
        local ec=0
        $pytest_cmd "$tdir" "${ignore_flags[@]}" -q --tb=short 2>&1 || ec=$?
        if [ $ec -eq 0 ] || [ $ec -eq 5 ]; then
            log_pass "${tdir} — 通过"
        else
            log_fail "${tdir} — 部分失败 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Python 测试全部通过"
}

# ============================================================================
# 前端检查
# ============================================================================

check_frontend_lint() {
    log_header "前端 Lint 检查"
    if [ ${#FRONTEND_DIRS[@]} -eq 0 ]; then
        log_skip "未发现已安装依赖的前端目录"; return
    fi

    local lint_cmd="pnpm lint"
    command -v pnpm &>/dev/null || lint_cmd="npm run lint"

    local all_pass=true
    for app_dir in "${FRONTEND_DIRS[@]}"; do
        local pkg="${app_dir}/package.json"
        [ "$app_dir" = "." ] && pkg="package.json"
        if ! grep -q '"lint"' "$pkg" 2>/dev/null; then
            log_skip "${app_dir} — 未定义 lint 脚本"; continue
        fi
        if (cd "$app_dir" && $lint_cmd 2>/dev/null); then
            log_pass "${app_dir} lint — 通过"
        else
            log_fail "${app_dir} lint — 发现问题"
            all_pass=false
        fi
    done
    $all_pass && log_pass "前端 lint 全部通过"
}

# ============================================================================
# Node.js 检查
# ============================================================================

check_nodejs_lint() {
    log_header "Node.js Lint 检查"
    if [ ${#NODEJS_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Node.js 项目"; return
    fi

    local all_pass=true
    for dir in "${NODEJS_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local pkg="${dir}/package.json"
        [ "$dir" = "." ] && pkg="package.json"

        if [ ! -f "$pkg" ]; then
            log_skip "${dir} — 无 package.json"; continue
        fi

        # 检查是否有 lint 脚本
        if ! grep -q '"lint"' "$pkg" 2>/dev/null; then
            log_skip "${dir} — 未定义 lint 脚本"; continue
        fi

        # 检查是否安装了依赖
        local node_modules="${dir}/node_modules"
        [ "$dir" = "." ] && node_modules="node_modules"
        if [ ! -d "$node_modules" ]; then
            log_fail "${dir} — 依赖未安装"
            echo -e "  ${YELLOW}⚠ 请运行: cd ${dir} && pnpm install (或 npm install)${NC}"
            all_pass=false
            continue
        fi

        # 运行 lint
        local lint_cmd="pnpm lint"
        command -v pnpm &>/dev/null || lint_cmd="npm run lint"
        local ec=0
        (cd "$dir" && $lint_cmd 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} lint — 通过"
        else
            log_fail "${dir} lint — 发现问题 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Node.js lint 全部通过"
}

check_nodejs_test() {
    log_header "Node.js 单元测试"
    if [ ${#NODEJS_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Node.js 项目"; return
    fi

    local all_pass=true
    for dir in "${NODEJS_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local pkg="${dir}/package.json"
        [ "$dir" = "." ] && pkg="package.json"

        if [ ! -f "$pkg" ]; then
            log_skip "${dir} — 无 package.json"; continue
        fi

        # 检查是否有 test 脚本
        if ! grep -q '"test"' "$pkg" 2>/dev/null; then
            log_skip "${dir} — 未定义 test 脚本"; continue
        fi

        # 检查是否安装了依赖
        local node_modules="${dir}/node_modules"
        [ "$dir" = "." ] && node_modules="node_modules"
        if [ ! -d "$node_modules" ]; then
            log_fail "${dir} — 依赖未安装"
            echo -e "  ${YELLOW}⚠ 请运行: cd ${dir} && pnpm install (或 npm install)${NC}"
            all_pass=false
            continue
        fi

        # 运行 test
        local test_cmd="pnpm test"
        command -v pnpm &>/dev/null || test_cmd="npm test"
        local ec=0
        (cd "$dir" && $test_cmd 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} test — 通过"
        else
            log_fail "${dir} test — 部分失败 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Node.js 测试全部通过"
}

# ============================================================================
# Go 检查
# ============================================================================

check_go_vet() {
    log_header "Go vet 检查"
    if [ ${#GO_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Go 项目"; return
    fi

    if ! command -v go &>/dev/null; then
        log_skip "go 未安装"; return
    fi

    local all_pass=true
    for dir in "${GO_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local ec=0
        (cd "$dir" && go vet ./... 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} go vet — 通过"
        else
            log_fail "${dir} go vet — 发现问题 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Go vet 全部通过"
}

check_go_test() {
    log_header "Go 单元测试"
    if [ ${#GO_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Go 项目"; return
    fi

    if ! command -v go &>/dev/null; then
        log_skip "go 未安装"; return
    fi

    local all_pass=true
    for dir in "${GO_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local ec=0
        (cd "$dir" && go test ./... -v 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} go test — 通过"
        else
            log_fail "${dir} go test — 部分失败 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Go 测试全部通过"
}

# ============================================================================
# Rust 检查
# ============================================================================

check_rust_clippy() {
    log_header "Rust clippy 检查"
    if [ ${#RUST_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Rust 项目"; return
    fi

    if ! command -v cargo &>/dev/null; then
        log_skip "cargo 未安装"; return
    fi

    if ! command -v cargo-clippy &>/dev/null; then
        log_skip "clippy 未安装 (运行: rustup component add clippy)"; return
    fi

    local all_pass=true
    for dir in "${RUST_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local ec=0
        (cd "$dir" && cargo clippy --all-targets --all-features -- -D warnings 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} clippy — 通过"
        else
            log_fail "${dir} clippy — 发现问题 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Rust clippy 全部通过"
}

check_rust_format() {
    log_header "Rust 格式检查 (rustfmt)"
    if [ ${#RUST_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Rust 项目"; return
    fi

    if ! command -v cargo &>/dev/null; then
        log_skip "cargo 未安装"; return
    fi

    if ! command -v rustfmt &>/dev/null; then
        log_skip "rustfmt 未安装 (运行: rustup component add rustfmt)"; return
    fi

    local all_pass=true
    for dir in "${RUST_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local ec=0
        (cd "$dir" && cargo fmt --check 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} rustfmt — 格式一致"
        else
            log_fail "${dir} rustfmt — 不一致 (运行 'cargo fmt' 修复)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Rust 格式检查全部通过"
}

check_rust_test() {
    log_header "Rust 单元测试"
    if [ ${#RUST_DIRS[@]} -eq 0 ]; then
        log_skip "未发现 Rust 项目"; return
    fi

    if ! command -v cargo &>/dev/null; then
        log_skip "cargo 未安装"; return
    fi

    local all_pass=true
    for dir in "${RUST_DIRS[@]}"; do
        [[ -z "$dir" ]] && continue
        local ec=0
        (cd "$dir" && cargo test 2>&1) || ec=$?
        if [ $ec -eq 0 ]; then
            log_pass "${dir} cargo test — 通过"
        else
            log_fail "${dir} cargo test — 部分失败 (exit=$ec)"
            all_pass=false
        fi
    done
    $all_pass && log_pass "Rust 测试全部通过"
}

# ============================================================================
# 主流程
# ============================================================================

main() {
    log_header "Agent 质量门禁 — $(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "  项目: ${PROJECT_ROOT}"
    echo -e "  分支: $(git branch --show-current 2>/dev/null || echo 'unknown')"

    detect_project_structure
    detect_changes

    echo -e "\n  变更检测: Python=$([ "$HAS_PYTHON_CHANGES" = true ] && echo 是 || echo 否)  " \
            "Node.js=$([ "$HAS_NODEJS_CHANGES" = true ] && echo 是 || echo 否)  " \
            "Go=$([ "$HAS_GO_CHANGES" = true ] && echo 是 || echo 否)  " \
            "Rust=$([ "$HAS_RUST_CHANGES" = true ] && echo 是 || echo 否)  " \
            "前端=$([ "$HAS_FRONTEND_CHANGES" = true ] && echo 是 || echo 否)"

    # 决定检查范围
    local run_python=$IS_PYTHON
    local run_nodejs=$IS_NODEJS
    local run_go=$IS_GO
    local run_rust=$IS_RUST
    local run_frontend=$IS_FRONTEND

    local has_any_changes=false
    $HAS_PYTHON_CHANGES || $HAS_NODEJS_CHANGES || $HAS_GO_CHANGES || $HAS_RUST_CHANGES || $HAS_FRONTEND_CHANGES && has_any_changes=true

    if ! $has_any_changes; then
        echo -e "\n  ${YELLOW}未检测到变更，基于探测结构运行全部适用检查${NC}"
    else
        $IS_PYTHON && ! $HAS_PYTHON_CHANGES && run_python=false
        $IS_NODEJS && ! $HAS_NODEJS_CHANGES && run_nodejs=false
        $IS_GO && ! $HAS_GO_CHANGES && run_go=false
        $IS_RUST && ! $HAS_RUST_CHANGES && run_rust=false
        $IS_FRONTEND && ! $HAS_FRONTEND_CHANGES && run_frontend=false
    fi

    # 运行检查
    $run_python && { check_python_lint; check_python_format; check_python_test; }
    $run_nodejs && { check_nodejs_lint; check_nodejs_test; }
    $run_go && { check_go_vet; check_go_test; }
    $run_rust && { check_rust_clippy; check_rust_format; check_rust_test; }
    $run_frontend && check_frontend_lint

    # 检查是否有任何检查被执行
    local any_check_run=false
    $run_python || $run_nodejs || $run_go || $run_rust || $run_frontend && any_check_run=true
    ! $any_check_run && log_skip "未检测到需要检查的项目类型"

    # ---- 汇总 ----
    log_header "质量门禁汇总"
    echo -e "  ${GREEN}通过: ${PASS}${NC}  ${RED}失败: ${FAIL}${NC}  ${YELLOW}跳过: ${SKIP}${NC}"

    {
        echo "质量门禁报告 — $(date '+%Y-%m-%d %H:%M:%S')"
        echo "分支: $(git branch --show-current 2>/dev/null || echo 'unknown')"
        echo "Python 源码: ${PYTHON_SRC_DIRS[*]:-无}  测试: ${PYTHON_TEST_DIRS[*]:-无}"
        echo "Node.js: ${NODEJS_DIRS[*]:-无}"
        echo "Go: ${GO_DIRS[*]:-无}"
        echo "Rust: ${RUST_DIRS[*]:-无}"
        echo "前端: ${FRONTEND_DIRS[*]:-无}"
        echo "通过: ${PASS}, 失败: ${FAIL}, 跳过: ${SKIP}"
        echo "结果: $([ $FAIL -eq 0 ] && echo 'PASSED' || echo 'FAILED')"
    } > "$REPORT_FILE"

    echo -e "  报告: ${REPORT_FILE}"

    if [ $FAIL -gt 0 ]; then
        echo -e "\n  ${RED}质量门禁未通过！${NC}"
        type vk_on_cleanup_failure &>/dev/null && vk_on_cleanup_failure
        exit 1
    else
        echo -e "\n  ${GREEN}质量门禁全部通过 ✓${NC}"
        type vk_on_cleanup_success &>/dev/null && vk_on_cleanup_success
        exit 0
    fi
}

main "$@"
