# terminal-bridge 指标「有目标但永远 down」：NetworkPolicy 挡住了 Prometheus

- 日期：2026-10-30
- 涉及：`deploy/helm/hci-platform/templates/terminal-bridge/networkpolicy.yaml`、`deploy/helm/hci-platform/values.yaml`、`deploy/helm/hci-platform/tests/terminal_bridge_networkpolicy_test.yaml`
- 关联：`2026-10-30-观测命名空间白名单与发布可用性度量.md`（去白名单之后才暴露出这条）

## 一、现场事实

| 事实 | 证据 |
| --- | --- |
| 去白名单后 staging 的 11 个业务目标全部被发现 | `activeTargets` 里 `hci-staging` 有 11 条 |
| 其余 10 个 `up=1`，只有 terminal-bridge `up=0` | `lastError: dial tcp 10.42.0.199:9999: connect: connection refused` |
| 端口不是问题：Pod 自身 readiness/liveness 都用 9999 且 Ready | 探针 `httpGet.port: http`(=9999)，Pod `ready=true` |
| 注解端口也不是问题：`prometheus.io/port=9999` 与 containerPort 一致 | Deployment 注解与 `ports` 对照 |
| 真正的原因是入站被策略拒绝：`terminal-bridge-allow-required` 只放行 Traefik | NetworkPolicy ingress 仅一条 `namespaceSelector=kube-system` + `podSelector=app=traefik` |

## 二、根因

k3s 的 NetworkPolicy 拒绝在客户端表现为 **connection refused**，不是常见的 timeout。这使得排查天然指向错误方向：「端口没监听」——而进程明明在监听，探针也在通过。

更根本的问题在设计顺序上：这条策略是按「浏览器只能经 Traefik 进来」写的，它描述的是**人类访问路径**；而 Prometheus 抓指标走的是 **Pod IP 直连**，是另一条访问路径。只删命名空间白名单（让目标被发现）而不补策略，结果就是「目标存在、永远 down」——比完全没目标更难发现，因为列表里看得见它。

## 三、修复

在 `ingress` 增加一条，只放行 observability 命名空间里 `app.kubernetes.io/name=prometheus` 这一个 Pod label，端口仍是 9999：

```yaml
- from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: hci-observability
      podSelector:
        matchLabels:
          app.kubernetes.io/name: prometheus
  ports:
    - protocol: TCP
      port: 9999
```

不放行整个 `hci-observability` 命名空间：该命名空间里还有 grafana / loki / tempo / langfuse，任何一个被攻破都不该能读 bridge 的 9999。

## 四、对抗性审查

| 攻击 | 结果 | 兜底 |
| --- | --- | --- |
| Prometheus 换了 Pod label | 指标又变 down | 选择器放在 `values.terminalBridge.networkPolicy.prometheusSelector`，改值即可；helm-unittest 已断言这条规则存在 |
| 有人把策略整体关掉「先让它通」 | 9999 对全集群开放 | 该开关 `networkPolicy.enabled` 本就有测试保护；本文档记录「关掉 = 放弃最小权限」 |
| 以为这是 NP 的普遍问题，给所有服务放开 | 扩大攻击面 | 其余 10 个服务所在命名空间实测无 default-deny（`kubectl get networkpolicy -A` 只有 argocd / hci-sim-staging / 本策略） |
| `hci-sim-staging` 的 default-deny 仍然存在 | 该环境的指标抓不到 | 已知边界，见观测文档第四节；本次不扩大范围 |

## 五、验收

```promql
up{job="hci-services", namespace="hci-staging", app="terminal-bridge"}  # 期望 1
count(up{job="hci-services", namespace="hci-staging"} == 1)             # 期望 11
```

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
| --- | --- | --- | --- |
| 2026-10-30 | v1.0 | 初版：入站策略漏 Prometheus 导致目标永远 down，补一条最小放行 + 单测 | 本文 |