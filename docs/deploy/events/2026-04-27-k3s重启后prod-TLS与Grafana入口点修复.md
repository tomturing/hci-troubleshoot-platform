---
status: active
category: deploy
audience: operator
last_updated: 2026-04-27
owner: team
---

# 2026-04-27：k3s 重启后 prod TLS 证书丢失与 Grafana 入口点不匹配修复

> **触发场景**：k3s 集群重启后 prod 环境（192.168.0.3）访问异常：
> - HTTPS 3443 返回 404
> - Grafana `/grafana` 显示 customer-ui 内容
> - admin-ui 监控页面 iframe 加载错误

## 1. 问题现象

### 1.1 外网访问异常
- `https://acli.sangfor.com.cn:3443/` 返回 404
- `https://acli.sangfor.com.cn:3443/grafana/` 显示 HCI 故障排查助手（customer-ui）
- 外网 NAT/防火墙端口映射正常，但 HTTPS 流量无法正确路由

### 1.2 内网测试现象
```bash
# 直接访问 Pod 正常
curl http://10.42.0.168:80  # → 200 OK

# 通过 Traefik 无 Host 头
curl -k https://127.0.0.1:3443/  # → 404

# 通过 Traefik 带 Host 头
curl -k -H "Host: acli.sangfor.com.cn" https://127.0.0.1:3443/  # → 200 OK
```

## 2. 根因分析（三层叠加）

### 2.1 第一层：TLS 证书丢失
- k3s 重启后 `prod-tls` Secret 丢失
- Ingress `spec.tls[0].secretName: prod-tls` 引用失败
- Traefik 日志持续报错：`"secret hci-prod/prod-tls does not exist"`

**对比 staging**：`staging-tls` Secret 存在，HTTPS 4443 正常

### 2.2 第二层：入口点配置不一致
**主站 Ingress 配置**：
- staging: `router.entrypoints: websecure`（与 TLS 配置匹配）
- prod: `router.entrypoints: web`（未更新，仍指向 HTTP 入口点）

**Grafana Ingress 配置**：
- staging: `router.entrypoints: websecure`（正确）
- prod: `router.entrypoints: web`（错误）

### 2.3 第三层：路由优先级与回退规则
当 Grafana Ingress 使用 `web` 入口点而主站使用 `websecure` 时：
1. HTTPS 请求只能匹配 `websecure` 入口点
2. Grafana Ingress（`web`）不被匹配
3. 请求被主站 Ingress 的 `path: /` 回退规则捕获
4. `/grafana` 显示 customer-ui 内容

## 3. 修复方案

### 3.1 恢复 TLS 证书
```bash
# 在 prod 集群 (192.168.0.3) 执行
# 1. 创建自签名证书
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /tmp/tls.key -out /tmp/tls.crt \
  -subj '/CN=acli.sangfor.com.cn'

# 2. 创建 TLS Secret
k3s kubectl create secret tls prod-tls \
  --cert=/tmp/tls.crt --key=/tmp/tls.key \
  -n hci-prod
```

### 3.2 修正主站 Ingress 入口点
```bash
# 确保主站 Ingress 使用 websecure 入口点
k3s kubectl patch ingress -n hci-prod hci-ingress \
  --type=merge \
  -p '{"metadata":{"annotations":{"traefik.ingress.kubernetes.io/router.entrypoints":"websecure"}}}'
```

### 3.3 修正 Grafana Ingress 入口点
```bash
# 确保 Grafana Ingress 与主站使用相同入口点
k3s kubectl patch ingress -n hci-observability grafana-ingress \
  --type=merge \
  -p '{"metadata":{"annotations":{"traefik.ingress.kubernetes.io/router.entrypoints":"websecure"}}}'
```

### 3.4 重启 Traefik 重新加载配置
```bash
k3s kubectl rollout restart deployment traefik -n kube-system
```

## 4. 验证步骤

### 4.1 基础验证
```bash
# 检查 Secret 存在
k3s kubectl get secret -n hci-prod prod-tls

# 检查 Ingress 配置
k3s kubectl get ingress -n hci-prod hci-ingress -o jsonpath='{.metadata.annotations}'
k3s kubectl get ingress -n hci-observability grafana-ingress -o jsonpath='{.metadata.annotations}'

# 检查 Traefik 日志
k3s kubectl logs -n kube-system deployment/traefik --tail=10 | grep -i error
```

### 4.2 功能验证
```bash
# 主站访问（带 Host 头）
curl -sk -H "Host: acli.sangfor.com.cn" https://127.0.0.1:3443/ | grep -i "<title>"

# Grafana 访问
curl -sk -H "Host: acli.sangfor.com.cn" https://127.0.0.1:3443/grafana/ | grep -i "<title>"
# 期望输出：<title>Grafana</title>
```

### 4.3 外网验证
- `https://acli.sangfor.com.cn:3443/` 显示 HCI 故障排查助手
- `https://acli.sangfor.com.cn:3443/grafana/` 显示 Grafana 登录页
- admin-ui 监控页面 iframe 正常加载

## 5. 经验总结与防回归措施

### 5.1 根本原因
- **prod HTTPS 期望值未被 Git 持续固化**：运行态虽然恢复了 `prod-tls` 与 TLS Ingress，但 env 仓库一度已回退为 HTTP，后续一旦按 Git 收敛就会再次失去 HTTPS
- **手动创建的 Secret 不能替代配置源修复**：只补 `kubectl create secret tls prod-tls` 只能恢复当下运行态，不能保证后续 ArgoCD/重启后的最终状态
- **环境配置不一致**：staging 与 prod 的 Ingress 配置存在差异
- **入口点自动切换机制未生效**：Helm Chart 的入口点自动切换逻辑依赖 `ingress.tls` 配置

### 5.2 防回归措施
1. **TLS 证书持久化**：
  - 将 prod HTTPS 访问地址与 `ingress.tls` 明确纳入 env 仓库管理
   - 或使用 CertManager 自动签发 Let's Encrypt 证书

2. **环境配置一致性检查**：
   ```bash
   # 定期对比 staging/prod 配置差异
   diff <(k3s kubectl get ingress -n hci-staging hci-ingress -o yaml) \
        <(ssh 192.168.0.3 "k3s kubectl get ingress -n hci-prod hci-ingress -o yaml")
   ```

3. **自动化验证脚本**：
   - 扩展 `scripts/k3s-verify.sh` 包含入口点一致性检查
   - 添加 HTTPS 端口的 Grafana 路由验证

4. **文档更新**：
   - 在避坑指南 `grafana.md` PIT-036 补充"环境重启后验证清单"
  - 明确 prod 与 staging 统一保留 HTTPS 的配置基线

### 5.3 相关避坑指南
- **PIT-036**：`/grafana` 被主站 `/` 回退路由吞掉
- **PIT-024**：Ingress 跨命名空间 Service 不可达（根因分析）
- **网络与服务异常排查专项指南 §二**：快速全景检查方法

## 6. 后续改进计划

| 优先级 | 改进项 | 负责人 | 完成时间 |
|--------|--------|--------|----------|
| P0 | 将 prod 保持 HTTPS 的 `publicUrl` 与 `ingress.tls` 固化到 env 仓库 | team | 2026-04-30 |
| P1 | 添加环境配置一致性检查脚本 | team | 2026-05-07 |
| P2 | 扩展 k3s-verify.sh 验证入口点 | team | 2026-05-14 |

---

**文档版本**: 1.0
**更新日期**: 2026-04-27
**验证状态**: ✅ 生产环境运行态已修复，Git 期望值已调整为继续保留 HTTPS
**关联 PR**: #234（文档与控制面修复），环境仓库需单独提交 prod HTTPS 配置变更