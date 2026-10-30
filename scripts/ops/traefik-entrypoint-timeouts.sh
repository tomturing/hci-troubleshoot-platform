#!/bin/bash
# =============================================================================
# Traefik entrypoint 读/写超时落地脚本
# =============================================================================
# 用途：把 deploy/config/traefik-entrypoint-timeouts.yaml 应用为 K3s
#       HelmChartConfig，给 websecure(对外 4443) 配 600s 读/写超时。
#
# 背景（Q2026102801062）：
#   Traefik 3.x 的 readTimeout 默认 60s，语义是「读完整个请求（含 body）」。
#   日志包分片上传单请求动辄数十秒，客户侧上行一慢就必然超时，网关返回 502，
#   桥侧把 502 当「平台暂时不可用」无限退避重试，最终停在固定分片（72/103）。
#   集群里只有 web(4888) 被人工改成 120s，websecure(4443) 一直是默认 60s。
#
# 使用方式：
#   ./scripts/ops/traefik-entrypoint-timeouts.sh [--dry-run]
#
# 验证：
#   kubectl get deploy traefik -n kube-system \
#     -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep websecure
#   kubectl exec -n kube-system deploy/traefik -- traefik version
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$PROJECT_ROOT/deploy/config/traefik-entrypoint-timeouts.yaml"
NAMESPACE="kube-system"
DEPLOYMENT="traefik"
DRY_RUN=false

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
echo "Traefik entrypoint 超时落地"
echo "=============================================="
echo ""

if [[ ! -f "$MANIFEST" ]]; then
    echo "错误: 清单不存在: $MANIFEST"
    exit 1
fi

if ! kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" > /dev/null 2>&1; then
    echo "错误: Traefik Deployment 不存在于 $NAMESPACE namespace"
    exit 1
fi

echo "当前 entrypoint 超时参数:"
echo "----------------------------------------------"
kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o json \
    | jq -r '(.spec.template.spec.containers[0].args // [])[]' \
    | grep -i "respondingTimeouts" || echo "（未配置，即使用 Traefik 默认值）"
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY-RUN] 将应用清单: $MANIFEST"
    echo "实际执行命令: kubectl apply -f $MANIFEST"
    exit 0
fi

echo "应用 HelmChartConfig..."
kubectl apply -f "$MANIFEST"

echo ""
echo "等待 K3s addon 重建 Traefik（HelmChartConfig 变更会触发 chart 重新安装）..."
# 注意：HelmChartConfig 的变更不是即时生效——addon 控制器要新建 helm-install job，
# 跑完才轮到 Deployment 滚动。只看 rollout status 会在「还没有变化」的旧 ReplicaSet
# 上立刻返回成功，从而误报配置未生效。因此这里直接轮询目标参数是否进了 args。
args_now() {
    kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o json 2>/dev/null \
        | jq -r '(.spec.template.spec.containers[0].args // [])[]' 2>/dev/null || true
}

for _ in $(seq 1 60); do
    if args_now | grep -q -- "--entryPoints.websecure.transport.respondingTimeouts.readTimeout=600"; then
        break
    fi
    sleep 3
done
kubectl rollout status deployment "$DEPLOYMENT" -n "$NAMESPACE" --timeout=180s

echo ""
echo "=============================================="
echo "验证结果"
echo "=============================================="
ARGS="$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o json \
    | jq -r '(.spec.template.spec.containers[0].args // [])[]')"

fail=0
for expect in \
    "entryPoints.websecure.transport.respondingTimeouts.readTimeout=600" \
    "entryPoints.websecure.transport.respondingTimeouts.writeTimeout=600" \
    "entryPoints.web.transport.respondingTimeouts.readTimeout=120" \
    "entryPoints.web.http.tls=true"; do
    if grep -q -- "--$expect" <<< "$ARGS"; then
        echo "✅ --$expect"
    else
        echo "❌ 缺少 --$expect"
        fail=1
    fi
done

# 反向校验：valuesContent 整体替换 Helm user values，漏写即回退。
# admin-ui 走 web 入口，丢掉 http.tls=true 会让管理后台退回明文 HTTP。

if [[ "$fail" -ne 0 ]]; then
    echo ""
    echo "配置未生效，请检查 K3s addon 状态："
    echo "  kubectl get helmchart traefik -n kube-system -o yaml | tail -20"
    exit 1
fi

echo ""
echo "=============================================="
echo "后续建议"
echo "=============================================="
echo "1. 观察网关 502 是否停止增长（PUT 到 case-service）："
echo "   kubectl exec -n kube-system deploy/traefik -- wget -qO- http://127.0.0.1:9100/metrics \\"
echo "     | grep 'traefik_service_requests_total{code=\"502\",method=\"PUT\"'"
echo ""
echo "2. 让现场重新点「上传到平台」即可续传：服务端已落盘的分片不会重传。"
echo "   参考文档: docs/verify/events/2026-10-30-日志包分片上传HTTP502与网关读超时验证.md"
