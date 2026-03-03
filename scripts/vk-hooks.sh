#!/usr/bin/env bash
# ============================================================================
# VK 工作流自动化钩子
# 用途: 在质量门禁通过/失败后自动更新 VK Issue 状态
#
# 设计:
#   - 由 agent-quality-gate.sh 在退出前调用
#   - 通过 VK REST API (PATCH /api/remote/issues/{id}) 更新状态
#   - Issue ID 来源优先级:
#     1. .vk/issue_id 文件（编排者在 start_workspace_session 时写入）
#     2. 环境变量 VK_ISSUE_ID
#   - VK 地址来源: 环境变量 VK_API_URL 或默认 http://127.0.0.1:9527
#
# 用法:
#   source scripts/vk-hooks.sh
#   vk_on_cleanup_success    # 质量门禁通过后调用
#   vk_on_cleanup_failure    # 质量门禁失败后调用
# ============================================================================

# VK API 基础地址
VK_API_URL="${VK_API_URL:-http://127.0.0.1:${PORT:-9527}}"

# ---- 内部函数 ----

# 获取当前 Workspace 关联的 Issue ID
_vk_get_issue_id() {
    # 优先级 1: .vk/issue_id 文件
    local project_root
    project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    local issue_file="${project_root}/.vk/issue_id"

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

# 调用 VK REST API 更新 Issue 状态
_vk_update_issue_status() {
    local issue_id="$1"
    local new_status="$2"

    local response
    response=$(curl -s -w "\n%{http_code}" -X PATCH \
        "${VK_API_URL}/api/remote/issues/${issue_id}" \
        -H "Content-Type: application/json" \
        -d "{\"status\": \"${new_status}\"}" 2>/dev/null)

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

# 质量门禁通过后调用 — 将 Issue 状态更新为 "In review"
vk_on_cleanup_success() {
    local issue_id
    if ! issue_id=$(_vk_get_issue_id); then
        echo -e "  \033[1;33m⚠\033[0m 未找到 VK Issue ID (.vk/issue_id 或 VK_ISSUE_ID)，跳过状态流转"
        return 0  # 非致命错误，不影响 cleanup 退出码
    fi

    echo -e "\n  \033[0;34m▸ VK 工作流钩子: cleanup 成功\033[0m"
    _vk_update_issue_status "$issue_id" "In review" || true
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
