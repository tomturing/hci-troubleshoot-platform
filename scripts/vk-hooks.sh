#!/usr/bin/env bash
# ============================================================================
# VK 工作流自动化钩子
# 用途: 在质量门禁通过/失败后自动更新 VK Issue 状态
#
# 设计:
#   - 由 agent-quality-gate.sh 在退出前调用
#   - 通过 VK REST API (PATCH /api/remote/issues/{id}) 更新状态
#   - REST API 要求 status_id（非状态名称），通过 .vk/status_map.json 解析
#   - Issue ID 来源优先级:
#     1. .vk/issue_id 文件（编排者在 start_workspace_session 时写入）
#     2. 环境变量 VK_ISSUE_ID
#   - VK 地址来源: 环境变量 VK_API_URL 或默认 http://127.0.0.1:9527
#
# 前置条件:
#   - .vk/status_map.json — 状态名→status_id 映射（编排者初始化项目时创建）
#   - .vk/issue_id — 当前 workspace 关联的 Issue UUID
#
# 用法:
#   source scripts/vk-hooks.sh
#   vk_on_cleanup_success    # 质量门禁通过后调用
#   vk_on_cleanup_failure    # 质量门禁失败后调用
# ============================================================================

# VK API 基础地址
VK_API_URL="${VK_API_URL:-http://127.0.0.1:${PORT:-9527}}"

# 项目根目录（相对于本脚本位置）
_VK_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- 内部函数 ----

# 获取当前 Workspace 关联的 Issue ID
_vk_get_issue_id() {
    # 优先级 1: .vk/issue_id 文件
    local issue_file="${_VK_PROJECT_ROOT}/.vk/issue_id"

    if [ -f "$issue_file" ]; then
        cat "$issue_file" | tr -d '[:space:]'
        return 0
    fi

    # 优先级 2: 环境变量
    if [ -n "${VK_ISSUE_ID:-}" ]; then
        echo "$VK_ISSUE_ID"
        return 0
    fi

    return 1
}

# 从 .vk/status_map.json 解析状态名到 status_id
# REST API PATCH /api/remote/issues/{id} 只接受 status_id，不接受状态名称
_vk_resolve_status_id() {
    local status_name="$1"
    local map_file="${_VK_PROJECT_ROOT}/.vk/status_map.json"

    if [ ! -f "$map_file" ]; then
        echo ""
        return 1
    fi

    # 用 python3 解析 JSON（避免依赖 jq）
    local status_id
    status_id=$(python3 -c "
import json, sys
with open('${map_file}') as f:
    m = json.load(f)
print(m.get('${status_name}', ''))
" 2>/dev/null)

    if [ -n "$status_id" ]; then
        echo "$status_id"
        return 0
    fi

    return 1
}

# 调用 VK REST API 更新 Issue 状态
# 注意: REST API 需要 status_id，通过 _vk_resolve_status_id 从 status_map.json 解析
_vk_update_issue_status() {
    local issue_id="$1"
    local new_status="$2"

    # 解析 status_id
    local status_id
    if ! status_id=$(_vk_resolve_status_id "$new_status"); then
        echo -e "  \033[1;33m⚠\033[0m 无法解析状态 '${new_status}' 的 status_id"
        echo -e "    请确认 .vk/status_map.json 存在且包含该状态"
        return 1
    fi

    local response
    response=$(curl -s -w "\n%{http_code}" -X PATCH \
        "${VK_API_URL}/api/remote/issues/${issue_id}" \
        -H "Content-Type: application/json" \
        -d "{\"status_id\": \"${status_id}\"}" 2>/dev/null)

    local http_code
    http_code=$(echo "$response" | tail -1)
    local body
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" = "200" ]; then
        echo -e "  \033[0;32m✓\033[0m VK Issue 状态已更新: → ${new_status}"
        return 0
    else
        echo -e "  \033[1;33m⚠\033[0m VK Issue 状态更新失败 (HTTP ${http_code}), 需手动更新"
        return 1
    fi
}

# ---- 公开钩子函数 ----

# 质量门禁通过后调用：
#   1. 更新 Issue 状态 → "In review"
#   2. 如果 .vk/auto_review.json 存在，自动创建交叉审查 Session
vk_on_cleanup_success() {
    local issue_id
    if ! issue_id=$(_vk_get_issue_id); then
        echo -e "  \033[1;33m⚠\033[0m 未找到 VK Issue ID (.vk/issue_id 或 VK_ISSUE_ID)，跳过状态流转"
        return 0  # 非致命错误，不影响 cleanup 退出码
    fi

    echo -e "\n  \033[0;34m▸ VK 工作流钩子: cleanup 成功\033[0m"
    _vk_update_issue_status "$issue_id" "In review" || true

    # 自动创建审查 Session（如果配置存在）
    _vk_auto_start_review "$issue_id" || true
}

# 自动创建交叉审查 Session
# 读取 .vk/auto_review.json 获取 repo_id、executor、prompt 等配置
# 配置格式:
#   {
#     "repo_id": "ca4e...",
#     "reviewer_executor": "CODEX",       // 交叉审查: Claude→Codex, Codex→Claude
#     "review_prompt_file": ".vk/prompts/reviewer.md"  // 可选
#   }
_vk_auto_start_review() {
    local issue_id="$1"
    local config_file="${_VK_PROJECT_ROOT}/.vk/auto_review.json"

    if [ ! -f "$config_file" ]; then
        echo -e "  \033[1;33m⚠\033[0m .vk/auto_review.json 不存在，跳过自动审查"
        return 0
    fi

    # 解析配置
    local repo_id reviewer_executor prompt_file
    repo_id=$(python3 -c "import json; c=json.load(open('${config_file}')); print(c['repo_id'])" 2>/dev/null)
    reviewer_executor=$(python3 -c "import json; c=json.load(open('${config_file}')); print(c['reviewer_executor'])" 2>/dev/null)
    prompt_file=$(python3 -c "import json; c=json.load(open('${config_file}')); print(c.get('review_prompt_file',''))" 2>/dev/null)

    if [ -z "$repo_id" ] || [ -z "$reviewer_executor" ]; then
        echo -e "  \033[1;33m⚠\033[0m auto_review.json 缺少 repo_id 或 reviewer_executor"
        return 1
    fi

    # 获取当前分支名（用作审查的 base_branch）
    local current_branch
    current_branch=$(git -C "${_VK_PROJECT_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ -z "$current_branch" ]; then
        echo -e "  \033[1;33m⚠\033[0m 无法获取当前分支"
        return 1
    fi

    # 构建审查提示词
    local prompt_arg=""
    if [ -n "$prompt_file" ] && [ -f "${_VK_PROJECT_ROOT}/${prompt_file}" ]; then
        prompt_arg="--prompt"
        local prompt_content
        prompt_content=$(cat "${_VK_PROJECT_ROOT}/${prompt_file}")
    fi

    echo -e "  \033[0;34m▸ 自动创建交叉审查 Session (${reviewer_executor})\033[0m"

    # 通过 MCP 客户端创建审查 Session
    local mcp_client="${_VK_PROJECT_ROOT}/scripts/vk-mcp-client.py"
    if [ ! -f "$mcp_client" ]; then
        echo -e "  \033[1;33m⚠\033[0m vk-mcp-client.py 不存在，跳过"
        return 1
    fi

    local ws_id
    local cmd="python3 ${mcp_client} --port ${PORT:-9527} start_review_session"
    cmd="${cmd} --repo-id ${repo_id}"
    cmd="${cmd} --base-branch ${current_branch}"
    cmd="${cmd} --issue-id ${issue_id}"
    cmd="${cmd} --title 'Review: ${current_branch} (${reviewer_executor})'"
    cmd="${cmd} --executor ${reviewer_executor}"

    if [ -n "$prompt_file" ] && [ -f "${_VK_PROJECT_ROOT}/${prompt_file}" ]; then
        # 读取 prompt 文件作为审查指令
        cmd="${cmd} --prompt '$(cat "${_VK_PROJECT_ROOT}/${prompt_file}" | head -100)'"
    fi

    ws_id=$(eval "$cmd" 2>/dev/null)
    if [ -n "$ws_id" ] && [ "$ws_id" != "ERROR"* ]; then
        echo -e "  \033[0;32m✓\033[0m 审查 Session 已创建: workspace=${ws_id}"
        echo -e "    分支: ${current_branch} → ${reviewer_executor} 审查"
    else
        echo -e "  \033[1;33m⚠\033[0m 审查 Session 创建失败，需手动创建"
    fi
}

# 质量门禁失败后调用（可选：保持 In progress 或标记为 blocked）
vk_on_cleanup_failure() {
    local issue_id
    if ! issue_id=$(_vk_get_issue_id); then
        return 0
    fi

    echo -e "\n  \033[0;34m▸ VK 工作流钩子: cleanup 失败\033[0m"
    # 失败时保持 In progress，不做额外操作
    echo -e "  Issue 保持当前状态 (In progress)"
}
