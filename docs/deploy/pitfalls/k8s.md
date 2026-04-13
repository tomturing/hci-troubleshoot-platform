# K8s / K3s / Helm 运维避坑

## PIT-014：Clash TUN 模式劫持 K8s ClusterIP 流量

**触发场景：** 宿主机开启 Clash TUN 模式，K8s Pod 间通过 Service（ClusterIP）调用时超时或断连。

**现象：** Pod IP 直连正常，Service DNS / ClusterIP 调用返回空响应（`Remote end closed connection without response`）。api-gateway 日志出现 `Server disconnected without sending a response`。

**根因：** Clash TUN 注入 `ip rule 9002: not from all iif lo lookup 2022`，将经过 iptables DNAT 后的 ClusterIP 包重定向到 Meta 虚拟网卡，绕过正常路由。

**本机已有永久修复：**
```bash
# 验证 bypass rules 存在
ip rule list | grep "priority 100"
# 应看到：
# 100: from all to 10.42.0.0/16 lookup main
# 100: from all to 10.43.0.0/16 lookup main
# 100: from 10.42.0.0/16 lookup main
```

若 rules 丢失（如系统重置了 ip rule），执行恢复：
```bash
sudo systemctl restart k8s-routing-bypass.service
```

**预防配置：**
- `/etc/systemd/system/k8s-routing-bypass.service`（开机自启）
- `~/.local/share/io.github.clash-verge-rev.clash-verge-rev/profiles/Merge.yaml`（Clash Verge TUN exclude-address）

---

## PIT-015：Helm release 卡在 pending-upgrade

**现象：** `helm list` 显示 `STATUS: pending-upgrade`，后续所有 upgrade 命令报错。

**根因：** `helm upgrade --wait` 超时（如 Pod 未就绪），release 被标记为 pending-upgrade 而非 failed。

**修复：**
```bash
# 查看历史，找最后一个 deployed 的 revision
helm history hci-platform -n hci-troubleshoot

# 回滚清除锁
helm rollback hci-platform <revision号> -n hci-troubleshoot

# 再次部署时不带 --wait（或加足够长的 timeout）
helm upgrade --install hci-platform ./deploy/helm/hci-platform \
  --namespace hci-troubleshoot \
  -f ./deploy/helm/hci-platform/values.yaml \
  -f ./deploy/helm/hci-platform/values-prod.yaml \
  -f ./.local/values-prod.override.yaml \
  --timeout 15m
```

**注意：** 本项目 `k3s-deploy-prod.sh` 默认带 `--wait`，在 Pod 未完全就绪时会触发此问题。

---

## PIT-016：K3s 镜像必须手动导入，不读取 Docker daemon

**现象：** Docker 镜像构建成功，`docker images` 可见，但 Pod 一直 `ImagePullBackOff` 或拉取旧镜像。

**根因：** K3s 使用独立的 containerd 实例（不是 Docker daemon），两者镜像存储完全隔离。

**修复：**
```bash
# 每次构建后必须导入
docker save <image>:<tag> | sudo k3s ctr images import -

# 或使用项目脚本（已集成 build+save+import）
IMAGE_TAG=<tag> bash scripts/k3s-build.sh

# 验证已导入
sudo k3s ctr images list | grep hci
```

---

## PIT-017：scheduler-service 重启次数虚高（RESTARTS 累计不清零）

**现象：** `kubectl get pods` 看到 scheduler-service `RESTARTS > 10`，误以为服务异常。

**根因：** K8s 的 RESTARTS 是累计值，不会清零。之前 OpenClaw 崩溃期间 scheduler 反复重试积累的历史次数。

**判断方式：**
```bash
# 看 AGE 和最后一次重启时间，而不是重启次数
sudo k3s kubectl get pods -n hci-troubleshoot
sudo k3s kubectl describe pod <scheduler-pod> -n hci-troubleshoot | grep "Last State\|Started\|Finished"
```
当前状态 `1/1 Running` 且上次重启时间超过 10 分钟即为正常。

---

## PIT-018：HostPath 挂载文件被截断（openclaw.json 等宿主机配置文件）

**现象：** Pod 启动时日志出现 `JSON parse error` / `unexpected end of file`，但宿主机文件看起来存在。

**根因：** 宿主机上的配置文件（如 `/home/node/.openclaw/openclaw.json`）在编辑过程中被截断，缺少末尾结构（如 `}`），导致容器内解析失败。

**排查：**
```bash
# 验证 JSON 完整性
python3 -c "import json; json.load(open('/home/node/.openclaw/openclaw.json'))" && echo "OK"

# 查看文件末尾
tail -5 /home/node/.openclaw/openclaw.json
```

**修复：**
```bash
# 如末尾缺 }
echo "}" >> /home/node/.openclaw/openclaw.json
python3 -c "import json; json.load(open('/home/node/.openclaw/openclaw.json'))" && echo "OK"
sudo k3s kubectl rollout restart deployment/openclaw -n hci-troubleshoot
```

**后续改进方向：** 将 openclaw.json 纳入 ConfigMap 管理，避免依赖手动维护的宿主机文件。

**OpenClaw 专属症状见：** `openclaw.md` PIT-013（JSON parse error → Pod CrashLoop 完整流程）

---

## PIT-019：HostPath 挂载 Pod 因 UID 不匹配无法读写宿主机目录

**现象：** Pod 日志出现 `permission denied` 访问挂载目录，或容器内写文件失败。

**根因：** Helm Chart 的 `securityContext.runAsUser` 与宿主机目录 owner UID 不一致。Ubuntu 默认第一个用户 UID=**1000**，但代码里写的是 1001。

**排查：**
```bash
# 确认宿主机用户 UID
id <username>
# 确认宿主机目录 owner
ls -lan /home/node/.openclaw/
# 确认 chart 中的 runAsUser
grep -r "runAsUser" deploy/helm/
```

**修复：** 将 `openclaw-service.yaml` 中 `runAsUser/runAsGroup/fsGroup` 改为与宿主机 node 用户一致（当前机器为 `1000`）。已在项目代码中修正。

## PIT-021：K3s Traefik 宿主机端口修改方法（避开 80/443 高危端口）

**场景：** 生产环境需将 Traefik 对外端口从 80/443 改为非特权端口（如 4888/4443），避开高危端口扫描限制或 NAT 规则限制。

**错误做法：** 直接 `kubectl patch svc traefik` 修改 port，升级 K3s 或 Traefik 时会被覆盖还原。

**正确做法：** 创建 `HelmChartConfig` 覆盖 Traefik Helm values，K3s 会持久保留：

```bash
cat << 'MANIFEST' | sudo tee /var/lib/rancher/k3s/server/manifests/traefik-custom.yaml
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    ports:
      web:
        exposedPort: 4888   # 宿主机对外端口
        port: 8000          # Traefik 内部端口（不变）
      websecure:
        exposedPort: 4443
        port: 8443
MANIFEST
```

K3s 约 10-30s 后自动 reconcile，无需重启。

**注意：**
- Ingress 注解 `traefik.ingress.kubernetes.io/router.entrypoints: web` 使用的是**内部 entrypoint 名称**，不是端口号，无需修改
- NAT/防火墙层需将 Hypervisor 端口映射目标改为 4888（原 80）
- Traefik Pod 内部端口（8000/8443）不受影响，集群内部访问无变化

## PIT-022：Helm DATABASE_URL 密码含特殊字符（@ # : 等）导致连接失败

**现象：** case-service / conversation-service / scheduler-service 启动后 API 返回 500，日志报 `password authentication failed` 或 `socket.gaierror: Name or service not known`。

**根因：** DATABASE_URL 通过 K8s env var 拼接：
```yaml
value: "postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres:5432/..."
```
密码含 `@`（如 `aihci@aclient2025`）→ URL 解析器以最后一个 `@` 为主机分隔符 → 用户名/密码被截断错误，认证失败。

**修复：** 改为在 Helm 模板渲染时用 `urlquery` 编码密码：
```yaml
value: {{ printf "postgresql+asyncpg://%s:%s@postgres:5432/%s"
    .Values.config.postgresUser
    (.Values.secrets.postgresPassword | urlquery)
    .Values.config.postgresDb | quote }}
```
`aihci@aclient2025` → `aihci%40aclient2025`，asyncpg/SQLAlchemy 会正确解码。

**规则：** 数据库密码、Redis 密码**禁止含** `@ : / # ? =` 等 URL 特殊字符，或必须在 Helm 模板中用 `urlquery` 编码后再拼 URL。

---

## PIT-038：Docker 容器端口映射外网访问 ERR_EMPTY_RESPONSE（Clash TUN 劫持 172.16/12）

**现象：** telnet 端口通、宿主机本地 curl 200、外网浏览器 `ERR_EMPTY_RESPONSE`。

**根因：** k3s + Clash TUN 共存时，`k8s-routing-bypass.service` 只为 k3s 的 `10.42/10.43` 添加了 bypass 规则，Docker 网段 `172.16.0.0/12`（含 `172.17/18/19...`）未加 bypass。外部流量经 iptables DNAT 转到 `172.18.x.x` 后，被 Clash rule 9002 劫持进 Meta TUN，无法到达容器，服务端直接 RST。

**修复：** 在 `/etc/systemd/system/k8s-routing-bypass.service` 的 ExecStart/ExecStop 中追加：
```
ip rule add priority 100 to 172.16.0.0/12 lookup main 2>/dev/null || true
ip rule add priority 100 from 172.16.0.0/12 lookup main 2>/dev/null || true
```
然后 `sudo systemctl daemon-reload && sudo systemctl restart k8s-routing-bypass`。

**验证：** `ip rule list | grep 172.16` 应看到两条 priority 100 规则。

---

## PIT-024：Traefik Ingress 无法跨命名空间引用 Service

**现象：** Ingress 中指定的 Service 名称在当前命名空间找不到（`Cannot create service: not found`），流量回退到优先级更低的路由规则，表现为访问 `/grafana` 等子路径返回其他服务（如 customer-ui）的内容。ExternalName Service 虽能创建但 Traefik 同样拒绝（`externalName services not allowed`）。

**根因：** Traefik Kubernetes Ingress Provider 要求 Ingress 资源和其引用的 Service 在**同一命名空间**。Ingress 在 `hci-troubleshoot`，但 grafana Service 在 `hci-observability`，Traefik 无法解析，整条路由规则被丢弃。

**正确方案：** 利用 Traefik 会扫描**全集群所有命名空间** Ingress 的特性，直接在 Service 所在命名空间（`hci-observability`）创建 Ingress，路由 `/grafana` → `grafana:3000`：
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grafana-ingress
  namespace: hci-observability   # 和 grafana Service 同一命名空间
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: web
spec:
  rules:
    - http:
        paths:
          - path: /grafana
            pathType: Prefix
            backend:
              service:
                name: grafana
                port:
                  number: 3000
```

**错误方案（不可用）：**
- ExternalName Service 桥接：Traefik 明确禁止（`externalName services not allowed`）
- 在 hci-troubleshoot 命名空间创建同名 ClusterIP：需手动维护 Endpoints IP，动态不稳定

**诊断命令：**
```bash
# 1. 看 Traefik 有无 Cannot create service 报错
k3s kubectl logs -n kube-system -l app.kubernetes.io/name=traefik --tail=50 | grep -E "ERR|grafana"

# 2. 验证 Service 是否在 Ingress 同一命名空间
k3s kubectl get svc grafana -n <ingress所在namespace>
```

## PIT-037：Clash TUN 宿主机上 Docker build 容器无法访问网络（apt-get / pip 超时）

> **注意：** npm install 场景的详细描述见 `frontend.md` **PIT-028**（权威条目），本条补充 apt-get / pip 等非 npm 场景。

**现象：** `docker build` 时 `RUN apt-get install` / `RUN pip install` 报：
```
ETIMEDOUT 198.18.x.x:443
```
即使配了国内 mirror（mirrors.ustc.edu.cn 等）也同样超时。

**根因：** 与 PIT-028 相同——Docker 构建容器默认使用独立 bridge 网络，Clash TUN fake-ip DNS 劫持导致容器内无法连通外网。

**修复：** 构建时加 `--network host`，让容器复用宿主机完整网络栈（走 Clash 代理）：
```bash
docker build --network host -t <image>:<tag> -f <Dockerfile> <context>
```

**适用范围：** npm/pip/apt/gem 等任何在 Clash TUN 宿主机上执行的 `docker build` + 包管理器网络请求。

**参见：** `frontend.md` PIT-028（完整根因分析）；`network-service-check.md` §二（Clash TUN 全景诊断）

---

## PIT-034：K3s Pod 无法访问外网 API（Clash fake-ip DNS 被劫持，pod 得到 198.18.x.x）

**触发场景：** K3s Pod 内向外部 AI/API 域名发起请求时超时或返回 400/503，宿主机 curl 同一地址 200 OK。

**现象特征：**
- 宿主机：`getent hosts open.bigmodel.cn` → `198.18.x.90` + curl HTTP 200 ✅（Clash TUN 正确代理）
- Pod 内：`getent hosts open.bigmodel.cn` → 同样 `198.18.x.90`，但 curl 超时 ❌
- Pod 日志（如 openclaw）："LLM request timed out."

**根因：** K3s Pod 的 bypass 规则 `ip rule 100: from 10.42.0.0/16 lookup main` 让 Pod 流量走 main 路由表，不经过 Clash TUN（`Meta` 设备，table 2022）。`198.18.x.x` 是 Clash 虚假 IP，只能通过 Meta TUN 到达，在 main 路由表中发往真实公网，连接失败。

**快速诊断：**
```bash
# 1. 查 Pod 解析结果
kubectl exec <pod> -- sh -c 'getent hosts <域名>'
# 若显示 198.18.x.x → Clash fake-ip 劫持

# 2. 确认 Clash TUN 设备存在
ip link show type tun 2>/dev/null   # 设备名通常为 Meta

# 3. 在宿主机确认同一域名也解析到 198.18.x.x
getent hosts <域名>
# 宿主机 curl 能通、pod 不通 → 是 bypass 规则阻止了 pod 走 TUN
```

**修复 A（推荐：容器级 DNS 覆盖，无需 sudo）：**  
给 Pod/Deployment 加 `dnsPolicy: None` + 真实 DNS（不被 Clash 管理，pod 流量经 `ip rule 100` 走 main 表，可直连 DNS 服务器）：
```yaml
spec:
  dnsPolicy: None
  dnsConfig:
    nameservers:
      - 114.114.114.114
      - 1.2.4.8
    options:
      - name: ndots
        value: "1"
```
本项目 openclaw Deployment 已在 [deploy/helm/hci-platform/templates/openclaw-service.yaml](deploy/helm/hci-platform/templates/openclaw-service.yaml) 固化此配置。

**修复 B（一次性修复全部 Pod，需 sudo）：**  
在 `k8s-routing-bypass.service` 中追加规则，让 Pod 到 Clash fake-ip 段也走 table 2022（Meta TUN）：
```bash
sudo ip rule add priority 95 from 10.42.0.0/16 to 198.18.0.0/15 lookup 2022
# /etc/systemd/system/k8s-routing-bypass.service 中也在 ExecStart 加同一行持久化
```

**注意：** 修复 A 会让该 Pod 丢失 K8s 集群 DNS（CoreDNS），服务发现失效。对于**只需访问外部 API 而不需要 k8s 服务名解析**的 Pod（如 openclaw）可以安全使用；对其他后端服务，用修复 B 或在 `nameservers` 中追加 CoreDNS IP（`10.43.0.10`）。

---

## HCI 环境健康检查清单（原 PIT-033，属 Runbook 非坑点）

**场景：** 每次开始测试前，或怀疑某个服务异常时，需要快速确认环境健康状态。

**一键验证命令：**
```bash
cd /aihci/hci-troubleshoot-platform && bash scripts/k3s-verify.sh
```
28/28 通过即为绿灯，可以开始测试。

**检查范围：**

| 类别 | 服务 | namespace |
|------|------|-----------|
| 存储 | postgres-0, redis-0 | hci-troubleshoot |
| 后端 | api-gateway, case-service, conversation-service, scheduler-service, kb-service | hci-troubleshoot |
| 前端 | customer-ui, admin-ui | hci-troubleshoot |
| AI | openclaw | hci-troubleshoot |
| 可观测性 | grafana, prometheus, loki, promtail, tempo | hci-observability |

**外部访问地址（Traefik 4888，路径路由模式）：**
```
http://acli.sangfor.com.cn:4888/         → customer-ui（客服端）
http://acli.sangfor.com.cn:4888/admin/   → admin-ui（管理端）
http://acli.sangfor.com.cn:4888/api/     → api-gateway（业务 API）
http://acli.sangfor.com.cn:4888/grafana  → Grafana 监控
```

**关键注意事项：**
- `api-gateway` 健康端点是 `/health`（不带 `/api` 前缀），容器内直接访问；通过 Ingress 路径 `/api/...` 透传不重写，`/api/cases/` 返回 422（缺参数）属正常，说明路由可达。
- `global.routingMode = "path"`（单 IP 环境），不能改为 `subdomain`，否则所有子域名 404。
- HPA 已启用：api-gateway（max 3）、case-service/conversation-service（max 6）会自动扩缩容。
- `learningclaw-0` 处于 `Init:ImagePullBackOff` 是预存问题，镜像未 import 到 K3s，不影响核心测试流程。
- `k3s-verify.sh` 第 3 节会自动创建并关闭测试工单（编号 Q20260310xxxxx），属正常行为。

---

## PIT-043：手动 kubectl apply 旧格式 Application 导致 releaseName 漂移

**触发场景**：排查 ArgoCD sync 失败时，用本地旧 yaml 文件手动重建 Application，导致 `releaseName` 与集群中已有 Deployment 的 selector 不一致。

**根本原因**：
- Helm 的 `app.kubernetes.io/instance` selector 由 `releaseName` 决定，**Deployment 创建后该字段不可变**
- `argocd-ops` Application 过去只管 `deploy/gitops/argocd-ops/` 目录，**`argo-apps/local/` 目录下的 Application 定义无 GitOps 守护**
- 任何人用错误 yaml `kubectl apply` 后，ArgoCD selfHeal 不会恢复（因为 Application 本身不在 GitOps 管理范围内）

**典型症状**：
```
spec.selector: Invalid value: {"matchLabels":{"app.kubernetes.io/instance":"hci-platform-dev",...}}:
field is immutable (retried 5 times)
```
Deployments 全部 Running、Healthy，但 SyncStatus = OutOfSync + SyncError。

**排查步骤**：
```bash
# 1. 确认 Application 的 releaseName
kubectl get application hci-platform-dev -n argocd -o json | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('releaseName:', d['spec'].get('source',{}).get('helm',{}).get('releaseName','(未设置，默认用 App name)'))
print('sources:', [s.get('helm',{}).get('releaseName') for s in d['spec'].get('sources',[])])
"

# 2. 确认现有 Deployment 的 selector
kubectl get deployment -n hci-dev -o jsonpath='{range .items[*]}{.metadata.name}: {.spec.selector.matchLabels}{"\n"}{end}'
```

**修复方法**：
```bash
# 用 Git 里正确的多源格式覆盖（不需要删除 Deployment）
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-dev.yaml
```

**根治方案（PIT-039 防护机制）**：
`argocd-ops` Application 已扩展为多源，同时监视 `argo-apps/local/` 目录：
- `selfHeal: true` 确保任何手动覆盖 5 分钟内自动恢复
- Git 里的 `argo-apps/local/*.yaml` 是唯一权威来源

**禁止操作**：
- ❌ 不得用本地备份/临时 yaml 直接 `kubectl apply` ArgoCD Application
- ❌ 不得 `kubectl delete application xxx` 后重建，必须通过 `kubectl apply -f deploy/gitops/argo-apps/local/` 操作
