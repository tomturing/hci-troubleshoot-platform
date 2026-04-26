# Grafana 避坑

## PIT-011：Grafana 登录后重定向到 localhost 导致无法访问

**现象：** 通过 IP 访问 Grafana，登录后跳转到 `http://localhost:3000`。

**根因：** `GF_SERVER_ROOT_URL` 被设置为 localhost 或未正确设置。

**修复：** 在 Helm values 中确认 `global.domain` 已正确设置，或通过 port-forward 访问（不受此问题影响）：
```bash
sudo k3s kubectl port-forward svc/grafana 3000:3000 -n hci-observability
# 访问 http://localhost:3000
```

## PIT-012：无域名部署时 Grafana Ingress 渲染出空 rules 导致 K8s Apply 失败

**现象：** `helm install/upgrade` 报错：`Ingress.networking.k8s.io "grafana-ingress" is invalid: spec: Invalid value: null: empty rules`

**根因：** Helm Chart 中 `templates/observability/ingress.yaml` 在 `global.domain` 为空时仍然渲染 Ingress 资源，但 rules 列表为空，K8s API 拒绝。

**修复：** 已在 Chart 模板外层加 `{{- if .Values.global.domain }}` guard（已提交项目代码）。无域名部署时 Ingress 资源不会被创建。

## PIT-020：无域名 IP 访问时 admin-ui 监控页面 Grafana URL 指向 localhost

> **关联关系：** PIT-036（路由层根因）→ PIT-024（Traefik 跨命名空间根因）→ PIT-020（前端 UI 层根因）是同一问题的三层递进。
> **完整排查顺序：** 先确认 `/grafana` 路由是否命中正确 Service（PIT-036），再看 Ingress 跨命名空间（k8s.md PIT-024），最后修 Vue 代码（PIT-020）。

**现象：** 通过 `http://<IP>/admin` 访问管理端，进入「系统监控」页面，iframe 加载 `http://localhost:3000` 导致白屏。

**根因：** `MonitoringView.vue` 中 `detectGrafana()` 只处理了 `admin.<domain>` 和 `localhost` 两种情况，IP 直连时 fallback 到 `localhost:3000`，即访问者自己机器的端口，而非服务器。

**修复方案（已提交）：**
1. **Ingress** 添加 `/grafana` 路径 → `grafana:3000`（`{{- if .Values.observability.enabled }}` 保护）
2. **Grafana Deployment** 在无域名时设置 `GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/grafana/` 和 `GF_SERVER_SERVE_FROM_SUB_PATH=true`
3. **MonitoringView.vue** IP 直连时使用 `window.location.origin + /grafana` 构造 URL

**注意：** 有域名部署时走 `grafana.<domain>` 子域名路由，不受影响。

---

## PIT-036：/grafana 被主站 / 回退路由吞掉，admin-ui 监控页显示 customer-ui

**现象：**
- admin-ui「系统监控」页面 iframe 显示的是 customer-ui（HCI 故障排查助手）。
- `curl http://<host>/grafana/` 返回 HTML 标题是 `HCI 故障排查助手` 而不是 `Grafana`。
- HTTPS 访问时 `/grafana` 显示 customer-ui 内容（Grafana Ingress 只监听 HTTP）。

**根因：**
1. **优先级问题**：路径路由模式下，Grafana 独立 Ingress 若未绑定主 Host 或优先级不足，Traefik 可能将 `/grafana` 命中到主站 Ingress 的 `path: /` 回退规则。
2. **入口点问题（2026-04 新增）**：当主站启用 TLS 时，主站 Ingress 使用 `websecure` 入口点（HTTPS），但 Grafana Ingress 仍使用 `web` 入口点（HTTP）。HTTPS 请求只会匹配 `websecure` 入口点的 Ingress，导致 Grafana Ingress 不匹配，被主站 `/` 回退规则捕获。

**永久修复（已验证）：**
1. 在 `templates/observability/ingress.yaml` 为 grafana-ingress 设置更高优先级：
	- `traefik.ingress.kubernetes.io/router.priority: “2000”`
2. 在 `routingMode=path` 下，grafana-ingress 使用与主站一致的 host（从 `global.publicUrl` 提取）。
3. **入口点自动切换（2026-04 新增）**：根据 `ingress.tls` 配置自动选择入口点：
	```yaml
	{{- if .Values.ingress.tls }}
	traefik.ingress.kubernetes.io/router.entrypoints: websecure
	{{- else }}
	traefik.ingress.kubernetes.io/router.entrypoints: web
	{{- end }}
	```
4. 在 values 固化参数：
	- `ingress.grafanaRouterPriority: “2000”`

**验证：**
```bash
# 1) 直接看页面标题（HTTP）
curl -s http://acli.sangfor.com.cn:4888/grafana/ | grep -i “<title>”
# 期望：<title>Grafana</title>

# 2) HTTPS 访问验证
curl -sk https://acli.sangfor.com.cn:4443/grafana/ | grep -i “<title>”
# 期望：<title>Grafana</title>

# 3) 运行平台验收脚本（已含命中内容校验）
cd /aihci/hci-troubleshoot-platform && bash scripts/k3s-verify.sh
```

**防回归：**
- 发布后必须执行 `k3s-verify.sh`，其 grafana 检查已升级为”校验内容来自 Grafana”，不再只看 HTTP 200。
- 启用 TLS 后必须验证 HTTPS 端口的 `/grafana` 路由正确。
