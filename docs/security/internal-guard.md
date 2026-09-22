# internal 管理面公网封禁（internal-guard）

> 关联：SRC-2026-5358（`acli.sangfor.com.cn` 管理面 internal 与离线诊断域多个接口完全无鉴权）
> 落地：PR security/src-5358-internal-isolation

## 1. 背景与根因

internal 模式下（`diagnosisService.identityMode=internal`），主 ingress 把**全部
`/api` 流量**先送 customer-ui，由 `frontend/customer/nginx.conf` 的 `location /api/`
无条件注入 `Authorization: Bearer <INTERNAL_API_TOKEN>` + 租户/角色身份头。
下游网关与 diagnosis-service 的令牌校验因此被"合法令牌"整体穿透：
**匿名请求 = customer-ui 服务身份 = 三角色管理员**，可匿名读写
`/api/internal/*` 管理面（采集器治理、审计、信任库、KBD 影响分析等）。

该缺陷与镜像版本无关（最新 P0 版镜像同样可复现），修复必须作用于**身份注入点
所在的流量拓扑**，而非继续加网关中间件（网关中间件早已存在且被注入穿透）。

## 2. 方案：独立 guard Ingress + Traefik ipAllowList

新增 `deploy/helm/hci-platform/templates/ingress-internal-guard.yaml`：

1. **Middleware** `{{ fullname }}-internal-guard`：`ipAllowList.sourceRange`
   来自 `ingress.internalGuard.sourceRange`（默认 RFC1918 三段内网）。
2. **guard Ingress** `{{ fullname }}-internal-guard`：与主 ingress 同 host、
   同 entrypoint，仅承载 `PathPrefix(/api/internal)`，backend 仍为 customer-ui，
   并经 annotation 绑定上述中间件。

工作原理：guard 规则 `Host(x) && PathPrefix(/api/internal)` 比主 ingress 的
`Host(x) && PathPrefix(/api)` **更长**，Traefik 按规则长度优先命中 →
`/api/internal` 流量先经 ipAllowList：公网源直接 403，内网源放行后行为与
现状完全一致。

### 为什么不能把 Middleware 直接挂在主 ingress 上

Traefik 的 `router.middlewares` annotation 是 **Ingress 级别**而非 Path 级别，
直接挂载会把公网客户对 `/api/cases` 等正常功能的访问一并拒绝。

### 已知实现坑（已固化到 CI 断言）

- Ingress annotation 的中间件引用格式是 **`<namespace>-<name>@kubernetescrd`
  （连字符拼接）**；斜杠格式仅适用于 IngressRoute spec。误用斜杠会导致
  traefik 报 `middleware does not exist` 且路由**静默回退**到主规则
  （请求仍 200，封禁完全失效，且不易察觉）。
- guard 必须与主 ingress **同 host**：host 不一致时 Traefik 不会命中 guard。

## 3. 残余风险（B1 落地后的现状）

> B1（[网关身份重签](./identity-resign.md)）已删除 customer-ui 的 Nginx 身份注入，
> 并将 `/api` 收回 api-gateway：匿名不再能通过 customer-ui 获得管理员身份，
> `/api/internal/*` 已由网关 `require_admin` 收敛。下表为**更新后**的残余风险。

| 风险 | 说明 | 后续根治 |
| --- | --- | --- |
| ~~内网源攻击者仍可经 customer-ui 注入身份访问 internal~~ | **B1 已根治**：注入已删除，`/api/internal/*` 需管理员凭证 | 已完成 |
| ~~`/api/diagnosis-sessions` 等非 internal 前缀仍暴露~~ | **B1 已根治**：客户身份由网关按 Cookie 重签为 `customer` 角色并做工单归属校验 | 已完成 |
| admin-ui Nginx 仍为 `/api` 注入服务端身份 | 管理端暂无独立登录凭证，"能访问 Admin UI 入口"即等同管理员 | 新增 `adminUI.ingress.internalGuard`（默认关闭）可收内网；根治靠管理员认证 + admin 域名收敛 |
| `ingress.internalGuard.enabled=false` 可整体关闭 | 关闭操作会同时失去 CI 渲染断言保护（断言按开关通过属预期） | 变更评审约束 |

## 4. 验证记录（2026-09-22，本机 staging）

- 模拟公网源（sourceRange 临时置为不可达网段 `203.0.113.0/24`）：
  `GET /api/internal/collectors` → **403**；
- 恢复 RFC1918 白名单：同请求 → **200**（内网行为不变）；
- 客户主链路回归：首页 200、`/api/cases` 307（P0 身份 Cookie 重定向，正常）。

## 5. CI 门禁

`scripts/verify/verify_internal_guard.py` 在 PR 上自动执行（ci.yml
「校验 internal 管理面公网封禁渲染契约」步骤），断言：

1. internal 模式默认渲染出 Middleware 与 guard Ingress，且配置正确；
2. guard 与主 ingress host 一致；
3. 主 ingress 的 `/api` 路径仍在（客户功能不受影响）；
4. 中间件引用为连字符格式（防止静默失效复发）；
5. `ingress.internalGuard.enabled=false` 时 guard 资源正确消失。

本地预检：`uv run python scripts/verify/verify_internal_guard.py`。

## 6. 后续路线

1. ~~**A2** 轮换 `INTERNAL_API_TOKEN`（secret 更新 + 全部消费方滚动）~~ ✅ 已完成；
2. ~~**B1** 网关 diagnosis 代理接入身份 Cookie 重签（与删除 Nginx 注入同一 PR）~~
   ✅ 已完成，见 [网关身份重签](./identity-resign.md)；
3. **B3** admin 域名收敛 + 管理员认证（P1）：消除"网络可达性 = 管理权限"。
