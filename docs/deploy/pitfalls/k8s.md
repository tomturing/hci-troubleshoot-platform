# K8s / K3s / Helm 运维避坑

## D-007：公网 HTTP 页面访问 localhost 被 PNA 阻止

> **触发场景：** 用户从公网域名访问系统，WebSocket 连接本地 bridge（ws://localhost:9999）失败。

**现象：**
- 本地 dev 环境（`http://hci.local`）正常连接 terminal_bridge
- 云端 staging 环境（`http://acli.sangfor.com.cn:4888`）WebSocket 连接失败
- 浏览器控制台报错：`WebSocket connection to 'ws://localhost:9999/' failed`
- fetch 测试显示：`The request client is not a secure context and the resource is in more-private address space 'loopback'`

**根因：** Chrome 104+ 引入 [Private Network Access (PNA)](https://developer.chrome.com/blog/private-network-access-preflight/) 安全机制：
- 非安全上下文（HTTP + 公网域名）禁止访问 localhost（私有网络）
- 这是浏览器硬性限制，CORS 头无法绕过

**判断方式：**
| 环境 | 页面协议 | 域名类型 | 安全上下文 | 访问 localhost |
|-----|---------|---------|----------|---------------|
| dev | HTTP | 本地域名 | ✅ 是 | ✅ 允许 |
| staging | HTTP | 公网域名 | ❌ 否 | ❌ 禁止 |
| staging | HTTPS | 公网域名 | ✅ 是 | ✅ 允许（需 PNA 预检） |

**解决方案：** 为公网环境启用 HTTPS

1. 生成自签名证书：
   ```bash
   openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
     -keyout /tmp/staging-tls.key \
     -out /tmp/staging-tls.crt \
     -subj "/CN=acli.sangfor.com.cn" \
     -addext "subjectAltName=DNS:acli.sangfor.com.cn"
   ```

2. 创建 K8s Secret：
   ```bash
   kubectl create secret tls staging-tls \
     --cert=/tmp/staging-tls.crt \
     --key=/tmp/staging-tls.key \
     -n hci-staging
   ```

3. 更新环境 values.yaml：
   ```yaml
   global:
     publicUrl: "https://acli.sangfor.com.cn:4443"

   ingress:
     tls:
       - secretName: staging-tls
         hosts:
           - acli.sangfor.com.cn
   ```

4. 访问地址变为 `https://acli.sangfor.com.cn:4443`（首次访问需接受自签名证书警告）

**相关文件：**
- `terminal_bridge/main.go`：CORS 头支持（PR #225, #226）
- `deploy/helm/hci-platform/templates/ingress.yaml`：TLS 入口切换（PR #227）

---

## D-006：GitHub PAT 失效导致 ghcr.io 镜像拉取失败（ImagePullBackOff）

> **⚠️ 高频问题，排查镜像拉取问题前首先检查此项！**

**触发场景：** Pod 处于 `ImagePullBackOff` 或 `ErrImagePull` 状态，特别是新部署或镜像 tag 更新后。

**现象：**
- Pod 状态 `ImagePullBackOff`
- `kubectl describe pod` 显示 `failed to authorize: failed to fetch anonymous token: unexpected status 401 Unauthorized`
- 节点上 `crictl pull` 失败，报 `401 Unauthorized`

**错误判断（易踩坑）：**
| 错误判断 | 实际情况 |
|---------|---------|
| Clash TUN / fake-ip 劫持 ghcr.io | 网络通，`curl https://ghcr.io/v2/` 返回 401（认证失败，非网络问题） |
| DNS 解析问题 | DNS 正常返回 IP，问题是认证 |
| 需要配置代理 | 代理不是根因，PAT 失效才是 |

**验证方法：**
```bash
# 1. 检查网络是否通（返回 401 表示网络通，认证失败）
curl -s -o /dev/null -w "%{http_code}" https://ghcr.io/v2/
# 预期输出：401（网络通，需要认证）

# 2. 验证 PAT 是否有效
curl -sI -u "tomturing:<your-token>" https://api.github.com/ | grep x-oauth-scopes
# 预期输出：x-oauth-scopes: read:packages

# 3. 测试 PAT 是否能获取 ghcr.io token
TOKEN="<your-token>"
curl -s "https://ghcr.io/token?scope=repository:tomturing/hci-troubleshoot-platform/api-gateway:pull&service=ghcr.io" -u "tomturing:$TOKEN"
# 预期输出：{"token":"..."}（有 token 表示 PAT 有效）

# 4. 检查当前集群中的 secret
kubectl get secret ghcr-pull-secret -n <namespace> -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | jq '.auths.ghcr.io.password'
```

**根因：** `hci-platform-env` 中的 `secrets.ghcrToken` 是过期或无效的 GitHub PAT，导致 ArgoCD 渲染的 `ghcr-pull-secret` 无法认证 ghcr.io。

**解决方案：**
1. 创建新的 GitHub PAT（需要 `read:packages` scope）
2. 更新 `hci-platform-env` 仓库中的 `secrets.ghcrToken`：
   ```yaml
   # hci-platform-env/environments/<env>/values.yaml
   secrets:
     ghcrToken: "ghp_xxxx"  # 新的 PAT
   ```
3. 推送后 ArgoCD 自动同步，Secret 更新
4. 删除旧的 ImagePullBackOff Pod，让 Deployment 创建新 Pod

**预防措施：**
- GitHub PAT 默认有效期 90 天，设置日历提醒定期更新
- 使用 GitHub App 或 fine-grained PAT 可获得更长的有效期
- 监控告警：添加镜像拉取失败的告警规则

---

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

## D-001：ArgoCD 多集群 App of Apps 分层管理 + 环境标识方式

### 背景

多集群 GitOps 场景下（dev + staging/prod），常见两个互相关联的问题：

**问题一：`cloud/` Application 被 dev ArgoCD 误管**

`argo-apps/cloud/` 里的 Application 目标 namespace 是 staging/prod 集群，如果 dev 集群的 ArgoCD 把这些文件 apply 进来，会尝试把 staging 的负载注册到 dev 集群的 ArgoCD，因为 dev ArgoCD 没有 staging 集群凭据，同步会一直失败。

**现象：** ArgoCD UI 出现大量 `Unable to connect to cluster` 或 `no such host` 告警，staging Application 状态永久 `Unknown`。

**根因：** 没有 App of Apps 分层，dev ArgoCD 的 `source` 路径误覆盖了 `cloud/` 目录。

**问题二：环境标识依赖可变标签，Copilot/Agent 无法自动判断当前环境**

通过 `kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'` 查询环境，但：
- 标签需要手动维护，新环境初始化时可能忘记打标签
- Copilot 终端 session 的 shell 环境初始化不完整，可能连到错误集群
- Agent 读不到标签时容易凭目录命名（`local/` = dev）做错误推断，导致在 staging 机器上执行了 dev 侧的改动

### 正确架构：严格分层

```
dev 集群
└── ArgoCD
    └── argocd-ops          # source: argocd-ops/ + argo-apps/local/
        ├── hci-platform-dev          → destination: dev 集群
        ├── hci-platform-data-dev     → destination: dev 集群
        └── hci-platform-obs-dev      → destination: dev 集群

staging 集群（或 Hub）
└── ArgoCD
    └── argocd-ops-staging  # source: argocd-ops/ + argo-apps/cloud/
        ├── hci-platform-staging      → destination: staging 集群
        ├── hci-platform-data-staging → destination: staging 集群
        └── hci-platform-obs-staging  → destination: staging 集群
```

### 修复方案

**步骤 1：拆分 App of Apps**

- dev 侧：`argocd-ops.yaml` 改为 `sources`，加入 `path: deploy/gitops/argo-apps/local`
- staging 侧：新建 `argo-apps/cloud/argocd-ops-staging.yaml`，sources 包含 `argo-apps/cloud/`

**步骤 2：bootstrap（仅首次，手动执行一次）**

```bash
# staging 机器上
kubectl apply -f deploy/gitops/argo-apps/cloud/argocd-ops-staging.yaml

# dev 机器上（argocd-ops.yaml 更新后 ArgoCD 会自动 reconcile，无需手动 apply）
```

**步骤 3：确保环境标签存在（防止 Agent 误判）**

```bash
# dev 机器
kubectl label ns argocd hci.env.role=dev --overwrite

# staging 机器
kubectl label ns argocd hci.env.role=staging --overwrite
```

### 关于环境识别的改进建议

`kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'` 是可行方案，但有以下限制：

| 限制 | 说明 |
|------|------|
| 标签可变 | 人为修改 namespace 标签即失效 |
| Agent 终端环境 | shell 初始化不完整时 kubeconfig 指向可能不同，读到空值 |
| 新环境遗漏 | namespace 创建时不自动打标签 |

更稳健的补充手段：将集群 context 名称命名为 `dev` / `staging`，通过 `kubectl config current-context` 辅助判断。两种方式并用，互为校验。

### 预防检查

Agent 在操作 `deploy/gitops/argo-apps/` 文件前，**必须先确认当前环境**：

```bash
kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'
# 或
kubectl config current-context
```

结果与操作目标不一致时，停止操作，告知用户确认。

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

---

## PIT-044：迁移体系切换后遗留触发器双倍计数

**场景**：从 Alembic/dbmate 迁移体系切换到 Atlas 声明式管理后，旧体系创建的数据库触发器和函数未被清理。

**症状**：
- DB 迁移 Job 历史成功，但 `conversation.message_count` 持续偏高（实际值的 2 倍）
- `kubectl exec postgres-0 -- psql ... -c "\d+ message"` 可见多个计数触发器共存
- `pb trigger JOIN pg_class` 查询显示同名功能的触发器 > 1 个

**根本原因**：
Alembic 迁移文件历史上创建了 `update_message_count_on_insert` 和 `update_message_count_on_delete` 触发器（调用 `update_conversation_message_count()` 函数）。切换到 Atlas 后，新迁移逻辑创建了替代触发器 `update_conversation_message_count`（调用 `fn_update_conversation_message_count()`），但旧触发器/函数未删除，两套逻辑并存导致每次 INSERT/DELETE 使 `message_count` ±2。

**排查步骤**：
```bash
# 检查 message 表触发器数量
kubectl exec -n hci-dev postgres-0 -- env PGPASSWORD=xxx psql -U hci_admin -d hci_troubleshoot \
  -c "SELECT tgname, relname FROM pg_trigger JOIN pg_class ON tgrelid=pg_class.oid WHERE NOT tgisinternal ORDER BY relname, tgname;"

# 若 message 表有超过 1 个 INSERT/DELETE 触发器，即为双倍计数
```

**修复方法**：
在 `database/desired_extras.sql` 头部添加幂等清理块，在触发器创建前先 DROP 遗留对象：
```sql
-- 清理遗留触发器（顺序不可颠倒：先 DROP 触发器，再 DROP 函数）
DROP TRIGGER IF EXISTS update_message_count_on_insert ON message;
DROP TRIGGER IF EXISTS update_message_count_on_delete ON message;
DROP TRIGGER IF EXISTS trigger_kbd_entry_updated_at ON kbd_entry;
DROP FUNCTION IF EXISTS update_conversation_message_count();
```

**预防**：
- 切换迁移体系时，必须在新迁移脚本中显式 DROP 旧体系创建的所有数据库对象
- `desired_extras.sql` 的幂等清理块涵盖所有已知遗留对象，下次 ArgoCD deploy 自动清理

---

## PIT-045：nginx 启动时 upstream DNS 解析失败导致 Pod crash

**触发场景：** nginx 容器作为反向代理，通过 `proxy_pass http://<service-name>:<port>` 访问 K8s 内部服务，在 Pod 启动阶段 DNS 尚未就绪或目标服务未注册时崩溃。

**现象：**
- nginx Pod 日志报错：`host not found in upstream 'api-gateway'`
- Pod 状态：`CrashLoopBackOff`，反复重启失败
- 容器启动后立即退出，无法进入 Running 状态

**根因：**
nginx 静态 upstream 在配置加载阶段（启动时）一次性解析 DNS，解析失败直接报错退出。K8s 环境中常见时序问题：
1. CoreDNS Pod 尚未就绪（镜像拉取慢、节点重启后 DNS 缓存丢失）
2. 目标 Service（如 api-gateway）尚未创建或 Endpoints 未注册
3. Pod 启动顺序不确定，nginx 可能先于依赖服务启动

**修复：** 使用动态 DNS 解析，让 nginx 在请求时而非启动时解析域名：
```nginx
# API 代理到 gateway（动态 DNS 解析，解决启动时 upstream 未就绪问题）
location /api/ {
    resolver 10.43.0.10 valid=30s;  # Kubernetes DNS Service IP
    set $upstream http://api-gateway:8000;
    proxy_pass $upstream;
    ...
}
```

关键配置说明：
- `resolver 10.43.0.10`：使用 K8s CoreDNS Service IP（固定值，各集群一致）
- `valid=30s`：DNS 缓存 30 秒，平衡性能与动态性
- `set $upstream ...`：必须通过变量间接引用，触发动态解析
- 直接 `proxy_pass http://api-gateway:8000` 仍为静态解析，改用变量才能动态

**适用范围：**
- nginx 反向代理 K8s Service 的所有场景
- WebSocket 代理（`/ws/` location）同样需要动态解析
- 多 replica 场景下无需逐一配置 upstream 服务器

**预防：**
- 前端 nginx.conf 中所有指向 K8s Service 的 `proxy_pass` 均使用动态解析模式
- 新增前端模块时参考 `frontend/admin/nginx.conf` 和 `frontend/customer/nginx.conf` 模板

---

## D-002：K3s 环境拉取 ECR 镜像失败（离线导入方案）

**触发场景：** ArgoCD 升级到 v3.3.6+ 后，Redis 镜像地址变为 `public.ecr.aws/docker/library/redis:8.2.3-alpine`，K3s 节点无法直接拉取 ECR 镜像。

**现象：**
- argocd-redis Pod 一直 `ImagePullBackOff`
- `k3s crictl pull public.ecr.aws/...` 报 `connection refused` 或超时
- Docker Desktop / WSL2 环境下 ECR 拉取需要特殊认证或代理

**根本原因：**
- AWS ECR Public 需要经过代理访问（在 WSL2/内网环境下）
- K3s containerd 的代理配置与 Docker 独立，Docker 能拉取不代表 K3s 能拉取

**修复（离线导入方案）：**
```bash
# 1. 用 Docker 拉取（Docker 走系统代理）
docker pull redis:8.2.3-alpine

# 2. 打 ECR 镜像标签
docker tag redis:8.2.3-alpine public.ecr.aws/docker/library/redis:8.2.3-alpine

# 3. 导出并导入 K3s containerd
docker save public.ecr.aws/docker/library/redis:8.2.3-alpine | sudo k3s ctr images import -

# 4. 验证
sudo k3s crictl images | grep redis
```

**预防：**
- ArgoCD 升级前检查新版本依赖的所有新镜像地址（`grep -r "image:" manifests/install.yaml | sort -u`）
- 若镜像源在 ECR / gcr.io 等特殊仓库，提前完成离线导入再执行升级
- 可在升级脚本中加入镜像预检步骤；若当前 `scripts/ops/argocd-upgrade.sh` 尚未实现该逻辑，请在升级前手动完成上述镜像查证与离线导入确认

---

## D-003：ArgoCD PreSync Job 依赖的 SA 鸡蛋问题

**触发场景：** App of Apps 模式下，argocd-ops Application 管理的资源中包含 ServiceAccount（SA），同时该 Application 的 PreSync Hook Job 需要使用这个 SA。

**现象：**
- PreSync Job 启动失败：`Error: serviceaccounts "argocd-repo-server-watchdog" not found`
- argocd-ops 永远无法完成第一次 Sync（SA 不存在 → PreSync 失败 → SA 永远不被创建）
- 删除 Application 重建也无法解决，因为 Job 启动早于 SA 创建

**根本原因：**
- ArgoCD Sync 顺序：PreSync Hook 最先执行，此时 Application 管理的主资源（包括 SA）尚未被 apply
- 第一次部署时，集群中没有这个 SA，Job 无法绑定 serviceAccountName

**修复（手动预创建 SA）：**
```bash
# 首次部署前手动创建 SA + RBAC（只需执行一次）
kubectl apply -f deploy/gitops/argocd-ops/argocd-repo-server-copyutil-watchdog.yaml

# 验证
kubectl get sa argocd-repo-server-watchdog -n argocd
```

**预防：**
- PreSync Hook Job 所需的 RBAC 资源（SA/Role/RoleBinding）**不应**由同一个 Application 管理
- 将 RBAC 资源分离到独立的 bootstrap 脚本或单独的 Application（无 PreSync 依赖）
- 或在 `argocd-upgrade.sh` 的 `post_upgrade_patch()` 步骤中预先 apply 这些资源（当前脚本已包含此逻辑）

---

## D-004：ArgoCD v3.x repo-server Redis EOF + K8s Pod git 网络（Clash TUN 环境）

**触发场景：** 两个独立问题在日志中都表现为 `EOF`，容易混淆：

### 问题一：Redis 连接池 EOF（非阻塞）

**现象：**
- repo-server 日志每隔几分钟出现：`Error attempting to retrieve git references from cache: EOF`
- 所有 Application 状态变为 `Unknown`，ComparisonError 为 `failed to list refs: EOF`

**根本原因：**
- go-redis 连接池复用空闲连接时，服务端已关闭该连接（TCP half-close），客户端读到 EOF
- ArgoCD v3.x repo-server 在 Redis cache 读取 EOF 时，不是静默 fallback，而是向上传播错误
- 非 TLS 问题（确认方式：`kubectl get secret argocd-redis -n argocd -o jsonpath='{.data}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(list(d.keys()))"` 只有 `auth` 字段则无 TLS）

**修复：**
```bash
# 1. 重启 repo-server 强制重建连接池
kubectl rollout restart deployment/argocd-repo-server -n argocd

# 2. 确保 argocd-cmd-params-cm 无 redis.tls.* 配置（纯密码模式）
kubectl get cm argocd-cmd-params-cm -n argocd -o yaml | grep redis

# 3. 触发新的 Sync（缓存重建后恢复正常）
argocd app sync argocd-ops --prune
```

**长效措施：** `argocd-repo-server-copyutil-watchdog.yaml` 中的 CronJob 定期重启 repo-server，防止长期积累空闲连接

### 问题二：K8s Pod 无法访问 GitHub（WSL2 + Clash TUN 环境）

**现象：**
- repo-server 内 `git ls-remote https://github.com/...` 超时
- `getent hosts github.com` 返回 `198.18.0.11`（Clash fake-IP）
- 从 WSL2 宿主机 git 可以访问，但 Pod 内不行

**根本原因：**
- Clash Verge 使用**虚拟网卡（TUN）模式**而非**系统代理**模式
- WSL2 eth0 流量经过 TUN 拦截，GitHub DNS 返回 fake-IP（`198.18.0.xx`），由 TUN 接管路由
- K8s Pod 走的是 flannel CNI 网络（非 eth0），TUN 无法拦截 flannel 流量
- 结果：Pod 拿到 fake-IP 后直接路由，无 TUN 代理，连接失败

**诊断命令：**
```bash
# 在 repo-server pod 内确认 DNS 返回的是 fake-IP
kubectl exec -n argocd <repo-server-pod> -- getent hosts github.com
# 若返回 198.18.0.xx → 是 fake-IP

# 测试 TCP 连通性
kubectl exec -n argocd <repo-server-pod> -- bash -c \
  "timeout 5 bash -c 'echo > /dev/tcp/198.18.0.11/443' && echo TCP:OK || echo TCP:FAIL"
# Pod 内 FAIL，宿主机 OK → 确认是 flannel 流量未被 TUN 拦截
```

**修复选项：**
1. **Clash 开启"局域网连接"** + 给 repo-server 配置 `HTTPS_PROXY=http://<windows-host-ip>:7897`（若 Windows 防火墙允许 WSL2 访问）
2. **使用 SSH 协议替代 HTTPS**：在 ArgoCD 仓库配置中改用 `git@github.com:...`，避免 HTTPS 代理问题
3. **在 WSL2 起 HTTP CONNECT 代理**（监听 cni0 `10.42.0.1`），让 Pod 流量走代理出去

**当前已知可用的临时解法：** 每次需要 Sync 时手动通过 API 触发（此时 ArgoCD 会复用已有的 git 缓存或短暂网络可达时完成 fetch）

**预防：**
- 新环境部署 ArgoCD 前，先从 repo-server Pod 内验证 GitHub 可达性：
  ```bash
  kubectl exec -n argocd <repo-server-pod> -- git ls-remote \
    https://github.com/<org>/<repo>.git HEAD
  ```

---

## D-005：ArgoCD PreSync/PostSync Hook 需使用包含目标工具的镜像

**触发场景：** ArgoCD PreSync/PostSync Hook Job 需要执行 `kubectl`、`helm`、`aws` 等外部命令。

**错误做法：**
- 使用 ArgoCD 官方镜像（`quay.io/argoproj/argocd:vx.y.z`）→ **不含 kubectl/helm**
- 使用 `latest` tag → 版本漂移，不可审计、不可复现
- 复制其他 Job 的镜像时未检查是否包含目标工具

**正确做法：**

| 工具 | 推荐镜像 | 说明 |
|------|---------|------|
| kubectl + shell | `bitnami/kubectl:latest` | 包含 kubectl 和 shell，latest 版本当前 v1.35.x |
| helm + shell | `alpine/helm` | 包含 helm 和 shell |
| aws CLI + shell | `amazon/aws-cli` | 包含 aws CLI 和 shell |

> ⚠️ **重要：**
> - `rancher/kubectl` 是纯 kubectl 静态二进制镜像，**不含 shell**，无法执行 `/bin/sh -c '...'` 脚本
> - `bitnami/kubectl` 只提供 `latest` 标签（无版本号标签），存在版本漂移风险，但当前版本符合 ±1 minor 策略
> - PreSync Hook Job 脚本需要 shell，必须使用包含 shell 的镜像

**版本偏移策略：**
```bash
# 查集群版本
kubectl version -o json | jq -r '.serverVersion.gitVersion'
# 例：输出 v1.34.5+k3s1 → 集群 minor = 34

# 版本选择规则（Kubernetes 官方支持范围）：
# - 最佳：kubectl minor = apiserver minor（如 v1.34.x）
# - 可接受：kubectl minor = apiserver minor ± 1（如 v1.33.x 或 v1.35.x）
# - 超出范围：kubectl minor 相差 ≥ 2（如 v1.32.x 与 v1.34相差 2 minor，超出支持）

# 查 rancher/kubectl 可用版本
curl -s "https://hub.docker.com/v2/repositories/rancher/kubectl/tags?page_size=20" | jq -r '.results[].name' | grep -v arm | grep -v amd

# 固定版本号示例
image: bitnami/kubectl:latest    # ✓ 正确：包含 shell + kubectl（当前 v1.35.x）
image: rancher/kubectl:v1.33.9   # ✗ 错误：纯 kubectl，不含 shell，无法执行脚本
image: bitnami/kubectl:1.31      # ✗ 错误：版本号标签不存在
```

**Hook Job 最佳实践：**
```yaml
spec:
  template:
    spec:
      activeDeadlineSeconds: 180  # 超时保护，需大于脚本内 --timeout（如 120s）+ 缓冲
      containers:
        - name: hook
          image: bitnami/kubectl:latest  # 包含 shell + kubectl
          imagePullPolicy: IfNotPresent
          resources:  # 资源限制
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 100m
              memory: 128Mi
```

> ⚠️ **超时配置注意：** `activeDeadlineSeconds` 应明显大于脚本内 `kubectl rollout status --timeout` 值，否则 patch 操作 + 日志输出消耗时间可能导致 Pod 在 rollout 等待完成前被强杀，Hook Job 误失败。

**参考案例：**
- PR#170 PreSync Hook 失败：使用 `quay.io/argoproj/argocd:v3.3.6`（不含 kubectl），导致 7 个 Error pods
- PR#195 初步修复：改用 `bitnami/kubectl:1.31` → **失败**（版本标签不存在，ErrImagePull）
- PR#196 版本偏移修复：改用 `rancher/kubectl:v1.33.9` → **失败**（不含 shell，无法执行脚本）
- PR#197 最终修复：改用 `bitnami/kubectl:latest`（包含 shell + kubectl）
- 若返回超时而非正常 refs，说明网络问题，先解决再触发 Sync
