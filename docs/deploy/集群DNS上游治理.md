---
status: active
category: deploy
audience: operator, architect
last_updated: 2026-11-02
owner: team
---

# 集群 DNS 上游治理（阻断节点透明代理 fake-ip 污染）

> **本文档回答的问题**：为什么集群里会出现「DNS 解析成功但连不上」，以及为什么修法必须是
> 「给 CoreDNS 显式声明上游」而不是「改 k3s 内置清单」。
>
> **不在此文档**：CoreDNS 的常规运维（扩缩容、指标）→ [部署指南.md](部署指南.md)

---

## 文档信息

- **版本**: 1.0
- **更新日期**: 2026-11-02
- **适用环境**: 节点主机上同时运行透明代理（Clash Verge / Mihomo，TUN + fake-ip 模式）的
  k3s 集群
- **落地对象**: `deploy/helm/hci-platform-dns` + `deploy/gitops/argo-apps/cloud/hci-platform-dns-staging.yaml`

---

## 1. 现象与本质

### 1.1 现象

应用侧报 TCP 建连超时，例如：

```
{"code":"AI_TIMEOUT","detail":"phase=connect, type=ConnectTimeout,
 url=POST https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"}
```

特征：**间歇性、成簇出现**。同一域名在 30 秒内连续失败，之后自行恢复；服务端日志里
没有 DNS 相关报错，`health` / `ready` 探针始终绿色。

### 1.2 本质：不是「DNS 故障」，而是「DNS 说谎」

| 维度 | 正常故障 | 本问题 |
|---|---|---|
| DNS 服务可用性 | 不可达 / 超时 | **完全健康**，应答 0.1s 级 |
| 应答内容 | 无 | **有，但落在 Pod 不可路由的地址空间** |
| health_check 能否发现 | 能 | **不能**（服务本身没坏） |
| 症状落点 | 解析阶段报错 | **建连阶段超时** |

因此所有「DNS 是否响应」维度的监控都无法发现它。唯一可靠的判据是：
**解析结果的地址空间必须落在 Pod 网络可路由的范围内**。

---

## 2. 根因链

1. 节点上运行 Clash（TUN + fake-ip，`fake-ip-range: 198.18.0.1/16`），启动时把自己的
   DNS 写入 systemd-resolved，节点 `/run/systemd/resolve/resolv.conf` 出现 `198.18.0.2`；
2. k3s 内置 Corefile 写的是 `forward . /etc/resolv.conf`，CoreDNS 的 `dnsPolicy: Default`
   让它在 **Pod 创建时**复制节点 resolv.conf 作为自己的上游；
3. `forward` 多上游默认 `policy random`，随机命中 `198.18.0.2` 时返回 fake-ip `198.18.0.x`；
4. CoreDNS `cache 30` 把这条污染应答缓存 30 秒，期间该域名的所有查询都返回 fake-ip；
5. `198.18.0.0/15` 只在宿主机 TUN 转发表内有意义，Pod 网段无法路由 → TCP SYN 无响应 →
   应用侧 `connect` 超时（`httpx` 默认 10s × 重试次数）。

### 2.1 两个容易误判的细节

- **「之前一直正常」不等于配置没变**：CoreDNS 的上游是**进程启动时的快照**，节点
  resolv.conf 后续被改不会被感知。污染可能潜伏很久，只在 CoreDNS Pod 重建的那一刻被激活。
- **宿主机 `dig` 的结果不可信**：TUN 会劫持本机 OUTPUT 链的 UDP 53，宿主机上无论问哪个
  上游都可能返回 fake-ip。判断必须以 **Pod 内实测**为准。

---

## 3. 为什么不能改 k3s 内置清单

`/var/lib/rancher/k3s/server/manifests/coredns.yaml` 属于 k3s **打包组件**。k3s 官方文档
《管理打包组件》明确：**「打包组件的清单由 K3s 管理，不应被更改」**。重启 k3s 服务或升级
k3s 时，k3s 会重新初始化该目录下的清单，**覆盖手工修改**。

同理，`kubectl edit configmap coredns` 也不是持久手段：k3s 的 addon 控制器会在后续
reconcile 中把内容还原成清单原文。

结论：**唯一被 k3s 认可的定制入口是 `coredns-custom` ConfigMap。**

---

## 4. 方案

### 4.1 生效原理（顺序敏感）

k3s 内置 Corefile 的 `.:53` 块：

```
.:53 {
    ...
    import /etc/coredns/custom/*.override
    forward . /etc/resolv.conf          ← 需要被屏蔽的内置行
}
```

`*.override` 的 import 位于内置 `forward` **之前**。CoreDNS 的 `forward` 是**终止插件**
（匹配到 zone 后不再调用后续插件），因此先注册的 forward 生效，内置的
`forward . /etc/resolv.conf` 被屏蔽。

### 4.2 关键参数

| 参数 | 取值 | 理由 |
|---|---|---|
| `policy` | `sequential` | CoreDNS 默认 `random`：任一上游返回坏数据即造成概率性故障。sequential 让行为可预测，配合 health_check 自动摘除故障上游 |
| `health_check` | `5s` | 低于 5s 在高延迟链路易误摘 |
| `max_fails` | `2` | 连续 2 次失败标记不可用 |
| `upstreams` | 3 个不同运营商 | 避免单一公网 DNS 故障导致外部域名整体不可解析；内网 `*.cluster.local` 由 `kubernetes` 插件直接应答，不受影响 |

### 4.3 刻意不做的事

- **不用 hostAliases 钉死 IP**：目标域名有 4 条 A 记录且会轮换，钉死会在轮换时整片故障；
- **不在 CoreDNS 里用 template 插件拦截保留网段**：只是掩盖症状，且会误伤合法的基准测试网段；
- **不关闭 Clash 的 fake-ip**：宿主机自身依赖它做域名分流，副作用远大于收益；
- **不在节点 resolv.conf 里删 `198.18.0.2`**：宿主机代理需要它；且 CoreDNS 已不读节点
  resolv.conf，改了也不影响集群。

---

## 5. 部署

### 5.1 资源

| 文件 | 说明 |
|---|---|
| `deploy/helm/hci-platform-dns/Chart.yaml` | Chart 元信息 |
| `deploy/helm/hci-platform-dns/values.yaml` | 上游列表与 forward 参数 |
| `deploy/helm/hci-platform-dns/templates/configmap-coredns-custom.yaml` | 渲染 `coredns-custom` |
| `deploy/gitops/argo-apps/cloud/hci-platform-dns-staging.yaml` | ArgoCD Application |

### 5.2 首次接管

若集群中已存在手工创建的 `coredns-custom`，ArgoCD 首次 sync 会覆盖其内容
（`metadata.labels` 以 Chart 渲染结果为准）。步骤：

```bash
# 1. 应用 Application（bootstrap 脚本会一并应用该目录）
bash scripts/ops/argocd-apply-apps.sh --scope cloud --yes

# 2. 观察同步结果
kubectl -n argocd get application hci-platform-dns-staging

# 3. 确认 ConfigMap 内容被接管
kubectl -n kube-system get cm coredns-custom -o jsonpath='{.data.forward\.override}'
```

### 5.3 使配置生效：必须重启 CoreDNS

`reload` 插件只对**主 Corefile** 做哈希比对，**不监控 `import` 进来的文件**。因此新增或
修改 `coredns-custom` 不会自动触发重载，必须重建 Pod：

```bash
kubectl -n kube-system rollout restart deployment coredns
kubectl -n kube-system rollout status deployment coredns
```

---

## 6. 验证（判据必须可证伪）

**不要用「页面能打开」「日志没有报错」作为验收依据**——污染是概率性的，日志采样极易漏判。

### 6.1 端到端连通性（唯一可信口径）

在业务 Pod 内模拟应用的「解析 → 建连」行为，跨多个 CoreDNS 缓存周期（30s）取样：

```bash
kubectl -n hci-staging exec deploy/diagnosis-service -- python3 -c "
import socket,time
h='token-plan.cn-beijing.maas.aliyuncs.com'
ok=fail=0; ips=set()
for i in range(30):
    try:
        ip=socket.gethostbyname(h); ips.add(ip)
        s=socket.socket(); s.settimeout(3); s.connect((ip,443)); s.close(); ok+=1
    except Exception as e:
        fail+=1; print(i,'FAIL',type(e).__name__,flush=True)
    time.sleep(1.5)
print('SUMMARY ok=%d fail=%d ips=%s'%(ok,fail,sorted(ips)))
"
```

| 结果 | 判定 |
|---|---|
| `fail=0` 且 `ips` 中无 `198.18.x` | 通过 |
| 出现连续多条 `FAIL`（成簇） | 未通过：污染窗口仍在 |
| `ips` 出现 `198.18.x` | 未通过：override 未生效或被绕过 |

### 6.2 补充校验

```bash
# 上游是否被替换（应返回真实 IP，而非 198.18.x）
kubectl -n kube-system logs deploy/coredns --tail=20

# 集群内部域名必须仍然正常（不应受本次变更影响）
kubectl -n hci-staging exec deploy/diagnosis-service -- \
  python3 -c "import socket;print(socket.gethostbyname('postgres.hci-staging.svc.cluster.local'))"
```

---

## 7. 失败模式与回滚

| 失败模式 | 现象 | 处置 |
|---|---|---|
| override 语法错误 | CoreDNS Pod `CrashLoopBackOff`，全集群 DNS 不可用 | `kubectl -n kube-system delete cm coredns-custom` 后重启 CoreDNS，回到继承节点 DNS（会恢复污染，但先恢复可用性） |
| override 未生效 | 解析结果再现 `198.18.x` | 检查 Corefile 是否仍有 `import /etc/coredns/custom/*.override`；检查 key 是否以 `.override` 结尾 |
| 上游不可达 | 外部域名解析失败，`*.cluster.local` 正常 | 检查 `upstreams` 网络可达性；`health_check` 会自动切换备用上游 |

**回滚**：`kubectl -n argocd patch application hci-platform-dns-staging --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'`
后手工删除 Application。注意 ConfigMap 带 `Prune=false`，删除 Application 不会连带删除
ConfigMap——这是刻意的：宁可残留，也不要静默回退到 fake-ip 污染。

---

## 8. 已知缺口

1. **缺黑盒探针**：这类「DNS 说谎」故障，CoreDNS 的 `health` / `ready` 与上游 `health_check`
   全部无法发现。建议补 blackbox-exporter 的 DNS 模块 + TCP/TLS 模块双探针，告警表达式用
   「`dns` 探针成功 且 `tcp` 探针失败」——这是唯一能识别该故障形态的信号；
2. **prod 未纳入**：prod 位于独立集群（`192.168.0.3:6443`），网络出口与 staging 不同，是否
   需要同样的显式上游需单独评估后再建 `hci-platform-dns-prod`；
3. **节点角色反模式仍在**：节点主机同时是开发工作站、k3s 节点与代理出口。长期应把 k3s 与
   桌面代理分离到不同主机/VM，属于架构层根治，成本高于本方案。

---

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-11-02 | v1.0 | 首次落地。将 staging 的 CoreDNS 上游从「继承节点 resolv.conf」改为显式 `223.5.5.5 / 119.29.29.29 / 114.114.114.114`（`policy sequential` + `health_check 5s` + `max_fails 2`），通过 k3s 官方扩展点 `coredns-custom` 承载，由 ArgoCD Application `hci-platform-dns-staging` 托管。背景：节点 Clash fake-ip 把 `198.18.0.2` 写入节点 DNS 上游，`forward` 默认 `random` 随机命中后返回 Pod 不可路由的 `198.18.0.x`，叠加 `cache 30` 形成 30 秒成簇失败窗口，实测修复前 30 次「解析→建连」失败 6 次（连续 27 秒），修复后 30/30 通过。**刻意否决**：改 k3s 内置清单（重启/升级会被覆盖）、hostAliases 钉死（A 记录轮换）、template 插件拦截保留网段（掩盖根因） |
