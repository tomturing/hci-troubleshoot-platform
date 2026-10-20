# B3 — Admin 域名收敛与 internalGuard 独立收窄（SRC-2026-5358 残余）

## 背景

- **B1 (#1071)**：删除 customer-ui 的 Nginx 身份注入，并将 `/api` 收回 api-gateway，关闭
  "匿名自动变管理员"的注入穿透面。
- **internal-guard (#1066)**：在 4443（websecure）新增独立 guard Ingress + `hci-platform-internal-guard`
  ipAllowList Middleware，公网源访问 `/api/internal` 直接 403。
- **残余盲区**：admin-ui（4888, web entrypoint, HTTPS）此前无任何 ipAllowList 护栏。其
  `internalGuard` 开关（PR #48 已在 env staging 开启）复用了 4443 的 `hci-platform-internal-guard`
  Middleware，sourceRange 与 4443 共用 `ingress.internalGuard.sourceRange`（默认 RFC1918 全段）。
  因此"网络可达 admin-ui = 管理员"的暴露面过大——要么依赖外部网络 ACL，要么对全部 RFC1918
  内网主机放行。

## B3 目标（本次范围）

仅做 **admin 域名收敛 + internalGuard 独立收窄到极小暴露面**，**不引入登录认证**（留待后续阶段）：

1. admin-ui 使用**独立域名**（如 `admin.acli.sangfor.com.cn`），与 customer 域明确区分。
2. admin-ui 的 ipAllowList 使用**独立** sourceRange（`adminUI.ingress.internalGuard.sourceRange`），
   与 4443 解耦——可单独收紧到极小暴露面（如仅办公网出口），而**不影响** 4443 公网
   `/api/internal` 的封禁策略。
3. 未配置 `adminUI.ingress.internalGuard.sourceRange` 时回退到 `ingress.internalGuard.sourceRange`，
   保持既有行为兼容。

## 实现

- `templates/ingress-internal-guard.yaml`：新增 `hci-platform-admin-guard` Middleware，
  sourceRange 优先取 `adminUI.ingress.internalGuard.sourceRange`，为空则回退
  `ingress.internalGuard.sourceRange`。
- `templates/admin-ui/ingress.yaml`：admin-ui-ingress 的 `router.middlewares` 引用
  `...-admin-guard` 而非 `...-internal-guard`。
- `values.yaml`：`adminUI.ingress.internalGuard.sourceRange: []`（默认空 → 回退）。

## 配置（env 仓库 staging）

```yaml
adminUI:
  ingress:
    host: admin.acli.sangfor.com.cn      # 独立管理域名（需 DNS/hosts 解析到 traefik LB 192.168.0.4:4888）
    internalGuard:
      enabled: true                       # 已由 PR #48 开启
      sourceRange:
        - <办公网出口 IP/CIDR>            # 极小暴露面：仅管理员可达
```

> 若 `sourceRange` 留空，则 admin 复用 `ingress.internalGuard.sourceRange`（RFC1918），行为与 B3 前一致。

## 验证

- `helm template` 渲染（带 `--set adminUI.ingress.internalGuard.sourceRange[0]=192.168.0.0/16`）
  应同时产出两个 Middleware：
  - `hci-platform-internal-guard`（4443）：sourceRange = RFC1918 全段（**不变**）
  - `hci-platform-admin-guard`（4888）：sourceRange = 配置的极小范围
- 集群验证：admin-ui-ingress annotation 出现 `...-admin-guard@kubernetescrd`；临时把 sourceRange
  收紧到不可达网段（如 `203.0.113.0/24`），4888 `/api/internal` → 403，恢复后 → 200。

## 残余风险

- 本次**未做登录认证**："网络可达 = 管理员"仅在"可达性"层面收窄到极小暴露面，仍未引入身份凭证。
  后续应接入 OIDC / MFA（B3 登录阶段）。
- DNS：独立域名需在内网解析到 traefik LB（`192.168.0.4:4888`）；未配置解析时该域名不可达。
