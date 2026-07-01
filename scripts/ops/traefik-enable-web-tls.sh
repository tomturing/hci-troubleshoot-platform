#!/bin/bash
# =============================================================================
# Traefik Web Entrypoint TLS 启用脚本
# =============================================================================
# 用途：为 admin-ui 内网隔离方案，启用 web entrypoint (端口 4888) 的 TLS
#
# 背景：
#   - 当前 Traefik 配置：web (4888) 无 TLS，websecure (4443) 有 TLS
#   - admin-ui 独立 Ingress 使用 web entrypoint
#   - 需启用 TLS 保证管理后台安全访问
#
# 使用方式：
#   ./scripts/ops/traefik-enable-web-tls.sh [--dry-run]
#
# 前置条件：
#   - Traefik Deployment 存在于 kube-system namespace
#   - 有 TLS Secret（可复用 staging-tls 或创建新的 admin-tls）
#
# 注意事项：
#   - Traefik 是 K3s 系统组件，由 Helm 管理
#   - 直接修改 Deployment 可能被 Helm 回滚，建议通过 Helm values 持久化
#   - 此脚本用于快速验证，持久化需更新 K3s Traefik Helm chart values
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NAMESPACE="kube-system"
DEPLOYMENT="traefik"
DRY_RUN=false

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "未知参数: $1"
            exit 1
            ;;
    esac
done

echo "=============================================="
echo "Traefik Web Entrypoint TLS 启用脚本"
echo "=============================================="
echo ""

# 检查 Traefik Deployment 存在
if ! kubectl get deployment $DEPLOYMENT -n $NAMESPACE > /dev/null 2>&1; then
    echo "错误: Traefik Deployment 不存在于 $NAMESPACE namespace"
    exit 1
fi

# 显示当前配置
echo "当前 Traefik entrypoint 配置:"
echo "----------------------------------------------"
TRAEFIK_ARGS="$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o json | jq -r '(.spec.template.spec.containers[0].args // [])[]')"
echo "$TRAEFIK_ARGS" | grep entryPoints || true
echo ""

# 检查是否已有 TLS 配置
CURRENT_TLS=$(echo "$TRAEFIK_ARGS" | grep "entryPoints.web.http.tls" || true)

if [[ "$CURRENT_TLS" == *"--entryPoints.web.http.tls=true"* ]]; then
    echo "✅ web entrypoint 已启用 TLS，无需修改"
    exit 0
fi

echo "⚠️  web entrypoint 当前未启用 TLS"
echo ""

# TLS Secret 检查
TLS_SECRET="${TLS_SECRET:-staging-tls}"
TLS_NAMESPACE="${TLS_NAMESPACE:-hci-staging}"

echo "检查 TLS Secret: $TLS_SECRET (namespace: $TLS_NAMESPACE)"
if ! kubectl get secret $TLS_SECRET -n $TLS_NAMESPACE > /dev/null 2>&1; then
    echo "警告: TLS Secret $TLS_SECRET 不存在于 $TLS_NAMESPACE namespace"
    echo "建议创建或指定其他 TLS Secret"
    echo ""
fi

# 执行修改
if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY-RUN] 将添加参数: --entryPoints.web.http.tls=true"
    echo ""
    echo "实际执行命令:"
    echo "kubectl patch deployment $DEPLOYMENT -n $NAMESPACE --type=json -p='[{\"op\": \"add\", \"path\": \"/spec/template/spec/containers/0/args/-\", \"value\": \"--entryPoints.web.http.tls=true\"}]'"
    exit 0
fi

echo "正在添加 TLS 参数..."
kubectl patch deployment $DEPLOYMENT -n $NAMESPACE --type=json -p='[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--entryPoints.web.http.tls=true"}]'

echo ""
echo "等待 Traefik Pod 重启..."
kubectl rollout status deployment $DEPLOYMENT -n $NAMESPACE --timeout=60s

echo ""
echo "=============================================="
echo "验证结果"
echo "=============================================="

# 验证配置
NEW_TLS=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o json | jq -r '(.spec.template.spec.containers[0].args // [])[]' | grep "entryPoints.web.http.tls" || true)

if [[ "$NEW_TLS" == *"--entryPoints.web.http.tls=true"* ]]; then
    echo "✅ web entrypoint TLS 已成功启用"
else
    echo "❌ TLS 启用失败，请检查"
    exit 1
fi

echo ""
echo "=============================================="
echo "持久化说明"
echo "=============================================="
echo "⚠️  此修改直接 patch Deployment，可能被 Helm 回滚"
echo ""
echo "持久化方法（推荐）:"
echo "1. 更新 K3s Traefik Helm chart values:"
echo "   helm -n kube-system upgrade traefik traefik/traefik --set 'entryPoints.web.http.tls=true'"
echo ""
echo "2. 或创建 K3s 配置文件 /var/lib/rancher/k3s/server/manifests/traefik-config.yaml"
echo ""
echo "参考文档: docs/deploy/admin-ui-internal-isolation.md"