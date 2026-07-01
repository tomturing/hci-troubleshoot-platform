# Admin UI 内网隔离方案

## 背景

当前架构中，admin-ui 和 customer-ui 通过同一入口暴露：

```
acli.sangfor.com.cn:4443
├── /api → api-gateway
├── /ws  → api-gateway
├── /admin → admin-ui  ← 公网暴露，依赖 IP 白名单
└── / → customer-ui
```

**问题**：admin-ui（管理后台）与 customer-ui（客户前台）绑定在同一入口，即使配置了 IP 白名单，管理后台仍附着在公网入口上。

**目标**：admin-ui 完全不暴露到公网，仅通过内网/VPN 可访问，customer-ui 继续使用现有公网入口。

---

## 方案设计

采用 **端口隔离 + 云厂商 IP 白名单** 方案：

```
┌─────────────────────────────────────────────────┐
│  云 LB: 443 → Traefik 4443 (websecure)          │
│  IP 白名单: 所有用户                             │
│  路由: customer-ui, /api, /ws                   │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  云 LB: 8443 → Traefik 4888 (web + TLS)         │
│  IP 白名单: 仅管理员 IP                          │
│  路由: admin-ui                                 │
└─────────────────────────────────────────────────┘
```

### 核心变更

| 项目 | 变更 |
|-----|------|
| **Traefik web entrypoint** | 启用 TLS（原为 HTTP） |
| **admin-ui Ingress** | 独立部署，使用 web entrypoint (4888) |
| **主 Ingress** | 移除 `/admin` 路径 |
| **云厂商 LB** | 新增 8443 端口映射 → 4888，配置管理员 IP 白名单 |

---

## 实施步骤

### 1. 启用 Traefik web entrypoint TLS

当前 Traefik 配置：

| Entrypoint | 内部端口 | Service 端口 | TLS |
|-----------|---------|-------------|-----|
| `web` | 8000 | 4888 | ❌ 无 |
| `websecure` | 8443 | 4443 | ✅ 有 |

执行脚本启用 TLS：

```bash
# 验证当前配置（dry-run）
./scripts/ops/traefik-enable-web-tls.sh --dry-run

# 实际执行
./scripts/ops/traefik-enable-web-tls.sh
```

**持久化方法**（推荐）：

```bash
# 方法一：Helm upgrade（需要 Traefik Helm repo）
helm -n kube-system upgrade traefik traefik/traefik \
  --set 'entryPoints.web.http.tls=true'

# 方法二：创建 K3s manifest 补丁
# /var/lib/rancher/k3s/server/manifests/traefik-config.yaml
```

### 2. 启用 admin-ui 独立 Ingress

更新环境 values.yaml：

```yaml
# hci-platform-env/environments/staging/values.yaml
adminUI:
  ingress:
    enabled: true
    entrypoint: web  # 使用 web entrypoint (4888)
    # TLS 配置（复用现有证书或创建新证书）
    tls:
      - secretName: staging-tls
        hosts:
          - acli.sangfor.com.cn
```

部署：

```bash
# ArgoCD 自动同步，或手动触发
kubectl apply -k environments/staging/
```

### 3. 云厂商配置

在云厂商负载均衡器配置：

1. **新增监听器**：端口 8443 (HTTPS) → 后端 192.168.0.4:4888
2. **IP 白名单**：仅允许管理员 IP（公司出口 IP + 家里 IP）
3. **健康检查**：TCP 或 HTTP 检查 4888 端口

### 4. 验证

```bash
# 检查 admin-ui Ingress
kubectl get ingress admin-ui-ingress -n hci-staging

# 检查 Traefik 路由
kubectl exec -n kube-system traefik-xxx -- traefik api routers

# 测试访问（需要管理员 IP）
curl -k https://acli.sangfor.com.cn:8443/
```

---

## 配置文件说明

### Helm Chart 变更

| 文件 | 变更 |
|-----|------|
| `templates/admin-ui/ingress.yaml` | 新增 admin-ui 独立 Ingress 模板 |
| `templates/ingress.yaml` | 移除 `/admin` 路径 |
| `values.yaml` | 新增 `adminUI.ingress` 配置节 |

### values.yaml 配置节

```yaml
adminUI:
  ingress:
    enabled: false  # 默认关闭，需在环境 values 中启用
    entrypoint: web # 使用 web entrypoint (端口 4888)
    host: ""        # 留空表示无 host 匹配（端口隔离场景）
    annotations: {}
    tls: []         # TLS 配置（可选）
```

---

## 访问方式变更

### 管理员访问 admin-ui

| 原方式 | 新方式 |
|-------|-------|
| `https://acli.sangfor.com.cn:4443/admin` | `https://acli.sangfor.com.cn:8443/` |
| 所有用户可达（IP 白名单过滤） | 仅管理员 IP 可达 |

### 客户访问 customer-ui

无变化：`https://acli.sangfor.com.cn:4443/`

---

## 回滚方案

如需回滚：

1. 禁用 admin-ui 独立 Ingress：
   ```yaml
   adminUI:
     ingress:
       enabled: false
   ```

2. 恢复主 Ingress `/admin` 路径（需手动修改模板）

3. 移除 Traefik web TLS 配置：
   ```bash
   kubectl patch deployment traefik -n kube-system --type=json \
     -p='[{"op": "remove", "path": "/spec/template/spec/containers/0/args/-", "value": "--entryPoints.web.http.tls=true"}]'
   ```

---

## 安全考虑

1. **TLS 必须启用**：管理后台不应使用 HTTP 明文传输
2. **IP 白名单**：云厂商层面配置，防止绕过 K8s 层面
3. **证书管理**：可复用现有 `staging-tls` 或创建专用 `admin-tls`
4. **审计日志**：建议在云厂商层面记录 admin-ui 访问日志

---

## 相关文档

- [部署指南](部署指南.md)
- [Traefik 配置脚本](../../scripts/ops/traefik-enable-web-tls.sh)
- [避坑指南 - K8s](pitfalls/k8s.md)