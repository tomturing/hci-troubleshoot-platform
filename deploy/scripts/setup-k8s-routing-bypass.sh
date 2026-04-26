#!/bin/bash
# =============================================================================
# K8s Routing Bypass Setup — Clash TUN 环境下 Pod DNS 修复
#
# 问题根因（PIT-034）：
#   Clash TUN 模式下，Pod DNS 查询返回 fake-ip（198.18.x.x）
#   但 Pod 无法通过 TUN 访问 fake-ip，导致服务间通信失败
#
# 解决方案：
#   添加 ip rule priority 100，让 K8s CIDR 流量优先走 main 路由表
#   绕过 Clash TUN，确保 Pod DNS 解析返回真实 ClusterIP
#
# 影响的 CIDR：
#   - 10.42.0.0/16  K3s Pod CIDR（flannel 默认）
#   - 10.43.0.0/16  K3s Service ClusterIP CIDR
#   - 172.16.0.0/12 Docker 容器网络（可选）
#
# 避坑指南：PIT-034（docs/deploy/pitfalls/k8s.md）
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="${SCRIPT_DIR}/k8s-routing-bypass.service"

echo "=== K8s Routing Bypass Setup ==="

# 检查是否已安装
if systemctl is-active k8s-routing-bypass &>/dev/null; then
    echo "[INFO] k8s-routing-bypass.service 已运行"
    systemctl status k8s-routing-bypass --no-pager || true
    exit 0
fi

# 检查是否在 Kubernetes 节点上
if ! command -v kubectl &>/dev/null && ! command -v k3s &>/dev/null; then
    echo "[WARN] 未检测到 kubectl/k3s，跳过安装（可能不在 K8s 节点上）"
    exit 0
fi

# 安装 systemd service
echo "[1/3] 复制 systemd service 文件..."
sudo cp "${SERVICE_FILE}" /etc/systemd/system/k8s-routing-bypass.service
sudo chmod 644 /etc/systemd/system/k8s-routing-bypass.service

echo "[2/3] 重载 systemd daemon..."
sudo systemctl daemon-reload

echo "[3/3] 启用并启动服务..."
sudo systemctl enable --now k8s-routing-bypass

# 验证
echo ""
echo "=== 验证 ==="
echo "ip rule bypass 规则："
ip rule list | grep 100 || echo "[WARN] 未找到 priority 100 规则"

echo ""
echo "服务状态："
systemctl status k8s-routing-bypass --no-pager || true

echo ""
echo "✅ k8s-routing-bypass.service 安装完成"