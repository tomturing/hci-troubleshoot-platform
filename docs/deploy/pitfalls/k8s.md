# K8s / K3s / Helm 运维避坑

## D-009：Helm chart 用 emptyDir 覆盖 `/etc/nginx/conf.d` 导致 nginx 启动无 server block

> **触发场景（场景 A — 无 templates）：** admin-ui / customer-ui 等基于 `nginx:alpine` 的静态前端服务，Dockerfile 直接将 nginx.conf COPY 到 `conf.d/`（baked），但 Helm chart 错误地用 emptyDir 挂载 `/etc/nginx/conf.d`，清空了 baked 配置。Pod 反复 CrashLoopBackOff，80 端口 connection refused。

> ⚠️ **另见 D-010**：若 Dockerfile 使用的是 templates 机制（COPY 到 `/etc/nginx/templates/`），emptyDir 挂载 conf.d **不会**导致 CrashLoopBackOff，而是白屏（项目配置未生效）。两种症状截然不同，务必先确认 Dockerfile 的 COPY 目标。

**现象（场景 A）：**
- Pod 处于 `CrashLoopBackOff` 或 `Running 0/1` 状态
- 日志显示 nginx worker 正常启动（`start worker process 20-35`），但立刻收到 SIGTERM 退出
- `kubectl describe pod` 事件：`Readiness probe failed: Get "http://10.42.x.x:80/": dial tcp 10.42.x.x:80: connect: connection refused`
- `kubectl exec <pod> -- ls /etc/nginx/conf.d/` 目录**为空**（无 default.conf）
- ArgoCD `hci-platform` 整体 health 变 Degraded，Deployment 状态 Synced 但集群里所有新 Pod 都不健康

**根因（场景 A — 无 templates）：**

Helm chart deployment 模板用 emptyDir 挂载 `/etc/nginx/conf.d`：

```yaml
volumes:
  - name: nginx-conf-d        # ← 元凶（场景 A 下）
    emptyDir: {}
volumeMounts:
  - mountPath: /etc/nginx/conf.d
    name: nginx-conf-d       # ← 清空了镜像 baked 的 default.conf
```

`nginx:alpine` 入口脚本 `20-envsubst-on-templates.sh` 的行为决策树：

```
/etc/nginx/templates/ 是否存在？
  ├── 不存在 → return 0，直接跳过，不写 conf.d  ← 场景 A
  └── 存在 → /etc/nginx/conf.d 是否可写？
             ├── 不可写（root:root 755 + uid=1000）→ return 0，静默跳过  ← D-010 场景
             └── 可写（emptyDir + fsGroup=1000）→ envsubst 渲染写入  ← 正常工作
```

**场景 A 的失败路径：** emptyDir 清空 conf.d → templates 不存在 → envsubst 跳过 → conf.d 为空 → nginx 无 server block → 80 端口无监听 → probe connection refused → CrashLoopBackOff。

**判断方式：**

```bash
# 第一步：快速区分 D-009 vs D-010 — 看 conf.d 是否为空
kubectl exec <pod> -n <ns> -- ls /etc/nginx/conf.d/
# 为空（无 default.conf）→ D-009 场景 A（无 templates + emptyDir 清空）
# 有 default.conf 但内容是官方默认配置 → D-010（详见 D-010 条目）
# 有 default.conf 且内容是项目配置 → 正常

# 第二步：确认 Dockerfile 使用的是哪种机制
kubectl exec <pod> -n <ns> -- ls -la /etc/nginx/templates/ 2>&1
# templates 不存在 → Dockerfile COPY 到 conf.d（baked），是场景 A
# templates 存在 → Dockerfile COPY 到 templates，见 D-010

# 第三步：确认 conf.d 是 emptyDir 挂载
kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.volumes}' | python3 -m json.tool | grep -A3 'conf-d'
```

**解决方案（场景 A）：**

删除 `nginx-conf-d` emptyDir volume 和对应 volumeMount，让镜像 baked 的 `default.conf` 正常生效：

```yaml
# 保留必要的 emptyDir（非 root 用户需要写权限）
volumes:
  - name: nginx-cache
    emptyDir: {}
  - name: nginx-run
    emptyDir: {}
  # ↑ 删除 nginx-conf-d 整段
volumeMounts:
  - name: nginx-cache
    mountPath: /var/cache/nginx
  - name: nginx-run
    mountPath: /run
  # ↑ 删除 mountPath: /etc/nginx/conf.d 那一行
```

`/var/cache/nginx` 和 `/run` 两个 emptyDir 挂载**必须保留**——uid=1000 对这两个目录无写权限，不挂载会因 `nginx.pid` 无法创建、cache 目录不可写而启动失败。

**相关文件：**
- `deploy/helm/hci-platform/templates/admin-ui/deployment.yaml`
- `deploy/helm/hci-platform/templates/customer-ui/deployment.yaml`

**错误判断（易踩坑）：**

| 错误判断 | 实际情况 |
|---------|---------|
| `runAsUser=1000` 导致 envsubst 写 conf.d 失败 → 应该加 emptyDir 挂载 conf.d 让其可写 | 若 Dockerfile 没有 templates，envsubst 直接跳过，加 emptyDir 只会清空 baked conf.d，使 D-009 更严重 |
| 加 emptyDir 后 envsubst 可以写入，问题应该解决 | **只有当 Dockerfile 也将 nginx.conf COPY 到 templates 时**，emptyDir + fsGroup=1000 才能让 envsubst 正常工作；两者必须配套，缺一不可 |
| nginx:alpine 镜像以 uid=101（nginx user）运行 → 应该把 `runAsUser` 改回 101 | 不能改。Chart 强制 `runAsUser=1000` 是 J-3 Pod 安全基线（OWASP K8s Top 10）|
| 镜像 tag 错误导致配置丢失 | 镜像正常，问题在 Chart 的空目录覆盖 |

---

## D-010：nginx:alpine templates 机制 + emptyDir + fsGroup 三要素缺一导致白屏

> **触发场景：** admin-ui / customer-ui Pod `1/1 Running`，readiness/liveness probe 通过，但浏览器访问 `/admin/` 或 `/` 白屏或 404，nginx 加载的是官方默认配置而非项目定制配置。

> ⚠️ **这是 2026-07-02 admin-ui 白屏事故的真实根因，由 PR #490 误删 PR #483 引入的关键挂载导致。详细事故时间线见「事故复盘」章节。**

**现象：**
- Pod `1/1 Running`，readiness/liveness probe HTTP 200
- `kubectl exec <pod> -- cat /etc/nginx/conf.d/default.conf` 内容是 **nginx:alpine 官方默认配置**（含 PHP FastCGI 注释），不是项目定制配置
- `kubectl exec <pod> -- ls /etc/nginx/templates/` 存在 `default.conf.template`，内容正确
- 启动日志出现：`20-envsubst-on-templates.sh: ERROR: ... is not writable`（或静默跳过）
- 访问 `/admin/` → 白屏 / 404

**nginx:alpine templates 机制的完整工作原理（第一性原理）：**

`nginx:alpine` 官方镜像设计了一套运行时配置渲染机制：将含有 `${VAR}` 占位符的模板放入 `/etc/nginx/templates/`，启动时由 `20-envsubst-on-templates.sh` 执行 `envsubst` 替换并写入 `/etc/nginx/conf.d/`。

**该机制在 K8s non-root（runAsUser=1000）环境下正常工作，需要三个要素同时成立：**

```
要素 1：Dockerfile 将 nginx.conf COPY 到 /etc/nginx/templates/
  COPY frontend/admin/nginx.conf /etc/nginx/templates/default.conf.template

要素 2：Helm Deployment 注入环境变量（nginx.conf 中的 ${VAR} 占位符）
  env:
    - name: DNS_RESOLVER
      value: "10.43.0.10"

要素 3：Helm Deployment 用 emptyDir 挂载 /etc/nginx/conf.d（使 uid=1000 可写）
  volumes:
    - name: nginx-conf-d
      emptyDir: {}
  volumeMounts:
    - name: nginx-conf-d
      mountPath: /etc/nginx/conf.d
```

**为什么 emptyDir 能让 uid=1000 写入 conf.d？**

Kubernetes 在挂载 emptyDir 时，会根据 Pod `securityContext.fsGroup` 设置目录的 GID 和 setgid bit：

```
# 挂载后 conf.d 的实际权限
drwxrwsrwx  2  root  1000  4096  /etc/nginx/conf.d
             ↑         ↑
         root 拥有   gid=1000（fsGroup）
                      ↑
              setgid bit（s）使组成员也可写

# uid=1000 属于 gid=1000 组，因此可以写入
```

**三要素缺一的后果：**

| 缺少的要素 | 症状 |
|-----------|------|
| 缺要素 1（没有 templates）| envsubst 脚本直接跳过，conf.d 保持 baked 内容——若同时缺要素 3，baked 内容正常；若有要素 3（emptyDir），conf.d 为空 → CrashLoopBackOff（D-009 场景 A）|
| 缺要素 2（没有 env 变量）| envsubst 运行但无法替换 `${DNS_RESOLVER}`，nginx.conf 中的变量名被当字面量写入，nginx 解析 `resolver` 指令时报错 → 启动失败 |
| 缺要素 3（没有 emptyDir）| conf.d 权限 root:root 755，uid=1000 不可写，envsubst 被跳过，conf.d 保持 nginx:alpine baked 的官方默认配置 → 白屏 ← **这正是 PR #490 的错误** |

**判断方式：**

```bash
# 快速诊断
kubectl exec <pod> -n <ns> -- sh -c '
  echo "=== conf.d ==="
  ls -la /etc/nginx/conf.d/
  echo "=== templates ==="
  ls -la /etc/nginx/templates/ 2>&1
  echo "=== conf.d content (first 3 lines) ==="
  head -3 /etc/nginx/conf.d/default.conf 2>/dev/null
'

# 正常状态（三要素均满足）
# conf.d/default.conf 由 uid=1000 在 2026-07-02 写入（不是镜像构建时间）
# 内容是项目定制配置（location /admin/ alias 等）
# 启动日志：「Running envsubst on .../templates/default.conf.template to .../conf.d/default.conf」

# 异常：缺要素 3（白屏根因）
# conf.d/default.conf 存在但是 nginx:alpine 官方配置（有 PHP/FastCGI 注释）
# 启动日志：「ERROR: /etc/nginx/templates exists, but /etc/nginx/conf.d is not writable」
```

**解决方案：** 确保三要素同时成立

**要素 1 — Dockerfile：**
```dockerfile
# templates 机制（含 ${DNS_RESOLVER} 占位符时使用）
COPY frontend/admin/nginx.conf /etc/nginx/templates/default.conf.template
```

**要素 2 — Helm deployment env：**
```yaml
env:
  - name: DNS_RESOLVER
    value: "10.43.0.10"  # K3s CoreDNS Service IP
```

**要素 3 — Helm deployment volumes（不可缺少）：**
```yaml
volumes:
  - name: nginx-cache
    emptyDir: {}
  - name: nginx-run
    emptyDir: {}
  - name: nginx-conf-d      # ← 必须有，使 conf.d 对 uid=1000 可写
    emptyDir: {}
volumeMounts:
  - name: nginx-cache
    mountPath: /var/cache/nginx
  - name: nginx-run
    mountPath: /run
  - name: nginx-conf-d      # ← 必须有
    mountPath: /etc/nginx/conf.d
```

**事故复盘（2026-07-02 admin-ui 白屏）：**

| 时间 | PR | 改动 | 效果 |
|------|----|----|------|
| Jun 25 | #481 | Dockerfile 改为 templates 机制 + nginx.conf 加 `${DNS_RESOLVER}` | 建立要素 1+2，但**此时无要素 3**，白屏 ❌ |
| Jun 25 | #483 | Helm 加 `nginx-conf-d` emptyDir | 补齐**要素 3**，三要素完整，修复白屏 ✅ |
| Jul 2 | #490 | Helm 移除 `nginx-conf-d` emptyDir（误认为是 D-009 根因）| 删除**要素 3**，白屏重现 ❌ |
| Jul 2 | #492 | Revert #490，重新加回 emptyDir | 要素 3 恢复，修复白屏 ✅ |

**PR #490 的误判链：**

```
错误推理：
  「D-009 说 emptyDir 覆盖 conf.d 是问题根源」
       ↓
  「admin-ui 白屏，conf.d 有问题」
       ↓
  「移除 emptyDir 可以修复」
       ↓
  ❌ 实际上移除 emptyDir 让 conf.d 恢复 root:root 755，
     uid=1000 无法写入，envsubst 被静默跳过，官方默认配置生效 → 白屏

正确推理：
  「conf.d 内容是官方默认配置（有 PHP 注释）」
       ↓
  「templates/ 存在 default.conf.template → Dockerfile 用的是 templates 机制」
       ↓
  「启动日志：conf.d is not writable → envsubst 被跳过」
       ↓
  「conf.d 权限 root:root 755，uid=1000 不可写」
       ↓
  ✅ 解决：加 emptyDir 挂载 conf.d（要素 3），使 fsGroup=1000 生效，uid=1000 可写
```

**相关文件：**
- `frontend/admin/Dockerfile` / `frontend/customer/Dockerfile`：要素 1
- `deploy/helm/hci-platform/templates/admin-ui/deployment.yaml`：要素 2+3
- `deploy/helm/hci-platform/values.yaml`：`workloadDefaults.securityContext.pod.fsGroup=1000`

**错误判断（易踩坑）：**

| 错误判断 | 实际情况 |
|---------|---------|
| D-009 说 emptyDir 覆盖 conf.d 是问题 → 移除 emptyDir 可以修复 | D-009 的前提是「无 templates」。有 templates 时，emptyDir 是**必要条件**，移除会触发白屏 |
| emptyDir 会清空 conf.d → nginx 没有任何配置 → 会 CrashLoopBackOff | 有 templates 时，envsubst 会重新填充 conf.d。不是 CrashLoop，而是白屏 |
| non-root 无法写 conf.d → 无论如何都跳过 | fsGroup=1000 + emptyDir 可以让 uid=1000 写入挂载目录。**原始 conf.d（root:root）不可写，挂载后的 emptyDir 可写** |
| 回滚 emptyDir 挂载 → 会 CrashLoopBackOff | 实际会白屏（templates 存在但被跳过，官方默认配置生效），不是 CrashLoop |
| Ingress/Traefik 路由配置错误 | Ingress 正确，问题在 Pod 内 nginx 配置 |

---

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
- `terminal_bridge/main.go`：CORS 头支持（PR #225, #226）— **仅在 HTTPS 场景下必要**，PNA 预检需要服务端返回 `Access-Control-Allow-Private-Network: true`
- `deploy/helm/hci-platform/templates/ingress.yaml`：TLS 入口切换（PR #227）— **真正的解决方案**，使页面成为安全上下文

**错误判断（易踩坑）：**
| 错误判断 | 实际情况 |
|---------|---------|
| 添加 CORS 头可以解决 HTTP 公网页面访问 localhost | HTTP 公网页面是"非安全上下文"，浏览器直接阻止，不发预检请求 |
| CORS 头对 HTTP 场景有效 | CORS 头仅在 HTTPS 场景下被浏览器检查 |

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

**⚠️ 重要：与 D-008（Clash 热重载问题）的区分**

| 场景 | 宿主机 DNS | Pod DNS | 宿主机连通性 | Pod 连通性 |
|------|-----------|---------|-------------|-----------|
| **PIT-034（本条目）** | 198.18.x.x | 同上 | ✅ | ❌ |
| **D-008（热重载）** | 真实 IP 或新 fake-ip | 旧 198.18.x.x | ✅ | ❌ |

**诊断命令：**
```bash
# 检查宿主机和 Pod DNS 是否一致
# 如果不一致 → D-008（重启 Clash）
# 如果一致但都不通 → PIT-034（本条目，路由规则问题）
```

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

---

## D-011：ArgoCD PreSync/PostSync Hook Job 失败后长期残留污染 Application Health

**触发场景：** ArgoCD Application 使用 PreSync/PostSync Hook Job 做集群引导（patch Deployment、改 ConfigMap 等），Job 失败后 Pod 已死但 Job 对象本身长期残留在命名空间，污染 Application Health 评估。

**症状：**
- Application `kubectl get application -n argocd` 显示 `Sync=Synced Health=Healthy`，**但** `.status.sync.message` 仍有：
  ```
  message: one or more synchronization tasks completed unsuccessfully
  Job has reached the specified backoff limit
  ```
- ArgoCD UI / `argocd app get` 同理显示历史失败 message
- 多年反复 Sync 都无法消除该 message（因为触发它的 Job 仍然存在）
- 与 `argocd.argoproj.io/tracking-id` 关联的资源被 ArgoCD 视为「有历史任务未完成」

**根因：**
- 默认的 `argocd.argoproj.io/hook-delete-policy: HookSucceeded` **只在 Hook 成功时**删除资源
- Job 失败时不会触发 HookSucceeded → 失败的 Job 永久残留
- ArgoCD Health 评估会扫描所有跟踪资源，看到 `status.conditions[type=Failed].status=True` 的 Job 就会把 `health.message` 标注为不健康

**正确做法：**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: my-presync-hook
  annotations:
    argocd.argoproj.io/hook: PreSync
    # 关键：失败时也清理，避免长期残留
    argocd.argoproj.io/hook-delete-policy: HookSucceeded,HookFailed
```

**保留策略选项（ArgoCD 官方支持组合）：**
| 值 | 行为 |
|----|------|
| `HookSucceeded` | Hook 成功后删除（默认） |
| `HookFailed` | Hook 失败后删除 |
| `BeforeHookCreation` | 同一 Hook 下次创建前删除旧资源 |
| 上述值可**逗号组合**，例：`HookSucceeded,HookFailed` 双向清理 |

**存量环境清理命令：**
```bash
# 1. 找出所有失败且 status.startTime 超过 1 天的 PreSync/PostSync Hook Job
kubectl get jobs -A -o json | jq -r '
  .items[] | select(
    (.metadata.annotations["argocd.argoproj.io/hook"] // "None") != "None"
    and (.status.conditions // []) | length > 0
    and any(.status.conditions[]; .type == "Failed" and .status == "True")
  ) | "\(.metadata.namespace) \(.metadata.name)"
'

# 2. 手工删除（确认 Pod 已 Completed/Failed 后）
kubectl delete job -n argocd <failed-hook-job-name>
```

**预防检查：**
- 新建任何 PreSync/PostSync Hook Job **必须**显式设置 `hook-delete-policy: HookSucceeded,HookFailed`
- 长期运行的 Hook（如 watch-dog）应使用 CronJob 而非 Job，避免状态污染
- CI/CD 流程可加扫描：定期清理 `status.conditions[type=Failed]=True` 的 Hook Job

**参考案例：**
- 2026-07-03 `argocd-ops` Application 持续显示 `Job has reached the specified backoff limit` message
- 根因：2026-06-05 失败的 `argocd-repo-server-probe-patch` PreSync Job 残留 28 天
- 修复：`kubectl delete job argocd-repo-server-probe-patch -n argocd` 立即清空 message
- 改进：v1.4 manifest 增加 `HookFailed` 策略，避免未来重蹈覆辙

---

## D-013：desired_schema.sql 声明式 schema 与 ORM 模型不同步导致 ORM 查询 500

**场景**：给 SQLAlchemy ORM 模型新增列，并写了 `database/atlas-migrations/` 版本化迁移文件，但**漏改 `database/desired_schema.sql`** 对应表定义。

**症状**：
- 读接口（用原生 SQL + 显式列名）正常返回
- 写接口 / 审核接口（用 ORM `select(Model)` 查全列）报 HTTP 500「未知错误」
- 现象不对称："打开编辑弹窗正常，点保存才 500"--因为 GET 详情用原生 SQL 不查新列，PATCH 保存用 ORM 查全列

**根本原因**：
`scripts/db-migrate.sh`（ArgoCD PreSync Hook 入口）只执行 `atlas schema apply --to desired_schema.sql`（声明式差量同步），**不执行** `atlas migrate apply`（版本化迁移）。因此 `desired_schema.sql` 是数据库 schema 的唯一 SSOT：
- ORM 模型定义了列 `X`，但 `desired_schema.sql` 没声明列 `X` -> 数据库无此列
- ORM `select(Model)` 生成 `SELECT ... X ...` -> PostgreSQL 报 `column "X" does not exist` -> 500
- 原生 SQL（`text("SELECT a, b FROM ...")`）不包含列 `X` -> 正常

**排查步骤**：
```bash
# 1. 对比 ORM 模型字段与数据库实际列（以 sop_document 为例）
kubectl exec -n <ns> postgres-0 -- sh -c 'psql -U $POSTGRES_USER -d hci_troubleshoot -c "\d sop_document"' | grep -i <列名>

# 2. 若数据库无该列，检查 desired_schema.sql 是否声明
grep -n "<列名>" database/desired_schema.sql

# 3. 检查 ORM 模型是否定义
grep -rn "<列名> = Column" backend/<service>/app/models/

# 4. 关键判据：ORM 模型有、desired_schema.sql 无 -> 即本坑
```

**修复方法**：
在 `desired_schema.sql` 对应表定义补齐：①列定义 ②`COMMENT ON COLUMN` ③索引（若有）。下次 ArgoCD Sync 触发 db-migrate PreSync Job，`atlas schema apply` 自动补列，无需手动改库。

**预防**：
- 改 ORM 模型新增/删除列时，**必须三处同步**：①ORM 模型 ②`desired_schema.sql`（声明式 SSOT）③`atlas-migrations/`（版本化迁移，如需留存变更记录）
- `desired_schema.sql` 是运行时数据库 schema 的**唯一权威**；`atlas-migrations/` 仅作为变更历史留存，不被 db-migrate.sh 应用
- Code Review 重点：PR 同时含 `models/*.py` 与 `atlas-migrations/` 但**不含** `desired_schema.sql` 时，高度怀疑漏改

**参考案例**：
- PR545 给 `SopDocument` 模型新增 `signals_json` 列并写了迁移文件 `20260714000000_add_signals_json_drop_steps_json.sql`，但漏改 `desired_schema.sql` 的 `sop_document` 表（仅改了 `kbd_entry`）
- staging 环境数据库 `sop_document` 表无 `signals_json` 列，导致 `PATCH /api/admin/sop/{id}`（编辑保存，ORM `select(SopDocument)`）500；`GET /api/v1/sop/{id}`（原生 SQL 显式列名）正常
- 受影响 7 处 `select(SopDocument)` ORM 查询：编辑保存、审核通过、发布、导入查重、抽取信号等

---

## D-014：db-seed 种子 SQL dollar-quote 闭定界符后漏逗号导致 PostSync Hook 失败

**触发场景**：修改 `database/seeds/*.sql` 中 `$TEMPLATE$...$TEMPLATE$` 包裹的 prompt 文案时，误删闭定界符 `$TEMPLATE$` 后的字段分隔逗号。

**症状**：
- ArgoCD `hci-platform-dev` Application `sync=Synced health=Healthy`，但 `operationState.phase=Failed`，message: `one or more synchronization tasks completed unsuccessfully (retried 5 times)`
- resource 列表：`Job/db-seed-<chartVer> hookPhase=Failed msg=Job has reached the specified backoff limit`
- Hook Job 已被 `hook-delete-policy: BeforeHookCreation,HookFailed` 清理，`kubectl get jobs` 看不到残留（区别于 D-011 的残留污染）
- 手动执行 seed SQL：`psql:...: ERROR: syntax error at or near "'1.0'"`，指向 version 字段

**根因**：
`02_system_prompts.sql` 等种子文件的 INSERT 元组结构为 `(stage, name, description, $TEMPLATE$...$TEMPLATE$, '1.0', TRUE)`，`$TEMPLATE$...$TEMPLATE$` 是 content_template 字段（dollar-quote 字符串）。**闭定界符 `$TEMPLATE$` 后必须紧跟逗号**分隔 version 字段。漏逗号时，dollar-quote 字符串与下一个单引号字符串 `'1.0'` 相邻，PostgreSQL 不允许这两种字符串字面量相邻拼接（不同于两个单引号字符串间的自动拼接），报 syntax error。psql 对长 INSERT 只报一个错误位置，常指向最后一个元组的 `'1.0'`，易误判为该元组问题，实为更早元组的闭定界符漏逗号导致字段错位。

**排查步骤**：
```bash
# 1. 确认 db-seed Hook 失败（Job 已被 HookFailed 清理，看 operationState）
kubectl get application hci-platform-dev -n argocd -o jsonpath='{.status.operationState.phase} {.status.operationState.message}{"\n"}'

# 2. 手动执行 seed SQL 复现（ON CONFLICT DO NOTHING 幂等，安全）
kubectl cp database/seeds/02_system_prompts.sql hci-dev/postgres-0:/tmp/02.sql
kubectl exec -n hci-dev postgres-0 -- psql -U hci_admin -d hci_troubleshoot -v ON_ERROR_STOP=1 -f /tmp/02.sql
# 报 syntax error at or near "'1.0'" 即本坑

# 3. 用正则把 $TEMPLATE$...$TEMPLATE$ 替换为 'X' 后再执行，错误转为
#    "VALUES lists must all have the same length" 即可定位到漏逗号的元组

# 4. 检查每个闭定界符后是否跟逗号
grep -n '\$TEMPLATE\$' database/seeds/02_system_prompts.sql
# 闭定界符行应以 $TEMPLATE$, 结尾；若 $TEMPLATE$ 后直接换行接 '1.0' 即漏逗号
```

**修复方法**：
在漏逗号的闭定界符后补逗号：`$TEMPLATE$` -> `$TEMPLATE$,`。下次 ArgoCD Sync 触发 db-seed PostSync Job 重新执行 seed SQL 成功，`operationState.phase` 恢复 `Succeeded`。

**预防**：
- `database/seeds/*.sql` 中 `$TEMPLATE$...$TEMPLATE$` 是 content_template 字段值，**闭定界符后必须跟逗号**（分隔 version 字段）
- 修改 prompt 文案时只改 `$TEMPLATE$` 之间的内容，勿动闭定界符后的逗号
- Code Review 重点：PR 改 `database/seeds/*.sql` 时，检查 diff 是否误删 `$TEMPLATE$,` 的逗号

**参考案例**：
- `02_system_prompts.sql` 的 `kbd_extract_signals_v2` prompt（PR611 v2 信号模型引入），第 678 行闭定界符 `$TEMPLATE$` 漏逗号
- PR611~PR613 期间 db-seed-010 PostSync Job 持续失败，但因 `health=Healthy` 未被及时发现；PR613 合并后排查 ArgoCD 异常时定位
- 修复：第 678 行 `$TEMPLATE$` -> `$TEMPLATE$,`

---

## D-015：Dockerfile 与构建脚本的 build context 基准不一致

**触发场景**：Dockerfile 使用 `COPY backend/...`、`COPY frontend/...` 等仓库根相对路径，但构建脚本把 `backend/` 或 `frontend/` 子目录传给 `docker build` 作为 context。

**症状**：

- `docker build --quiet` 长时间没有输出，看似卡在第一张镜像；
- 子目录没有自己的 `.dockerignore` 时，Docker 会先发送其中的 `.venv`、缓存和构建产物，耗时和空间异常增加；
- 上下文发送完后才报 `COPY failed`、`not found` 或 BuildKit 的 `failed to calculate checksum`；
- 同一个 Dockerfile 在仓库根手工构建成功，在脚本中失败。

**根因**：`COPY` 源路径只能访问 build context 内的文件，并且相对路径以 context 根为基准。Dockerfile 路径由 `-f` 指定，不会改变 `COPY` 的基准目录。以 `backend/` 为 context 时，`COPY backend/shared /app/shared` 实际寻找的是 `<repo>/backend/backend/shared`。

**正确做法**：

```bash
# Dockerfile 的 COPY 以仓库根为基准，因此最后一个参数必须是仓库根。
docker build \
  -f "${PROJECT_ROOT}/backend/api-gateway/Dockerfile" \
  -t hci-api-gateway:dev-local \
  "${PROJECT_ROOT}"
```

构建清单中的主仓服务应统一使用 `PROJECT_ROOT` context；只有来自独立仓库、且 Dockerfile 的 `COPY` 也以该独立仓库为基准时，才使用其他 context。

**预防检查**：

- 修改 Dockerfile 或构建清单时，对照每条 `COPY` 的第一个路径检查 context 是否包含它；
- 根 `.dockerignore` 只在仓库根作为 context 时生效，不能假设它会自动作用于子目录 context；
- CI 至少对一张 Python、一张前端和新增运行时镜像执行真实构建，Helm lint 无法发现此类问题；
- 无输出的 `--quiet` 构建先用 `ps` 检查是否仍在发送 context，排障时可临时去掉 `--quiet`。

**参考案例**：2026-07-27 dev K3s Terminal Bridge P0 部署时，`scripts/ops/k3s-build.sh` 的 Python/前端清单使用子目录 context，而对应 Dockerfile 均使用仓库根相对 `COPY`。统一到仓库根后恢复缓存过滤和可构建性。

---

## D-017：日志采集器首次启动回放过期 CRI 日志导致 Loki 整批拒绝

**触发场景**：以 Alloy 替换 Promtail，或清空 Alloy positions 后首次挂载已有的 `/var/log/pods`。节点上保留的 CRI 日志早于 Loki 的 `reject_old_samples_max_age`，而文件采集器默认从文件头读取。

**症状**：

- Alloy DaemonSet Ready、River 配置解析成功，但日志持续出现 `final error sending batch`；
- Loki 返回 HTTP 400，错误包含 `timestamp too old` 和 `oldest acceptable timestamp`；
- 新日志可能与过期日志同批发送，导致迁移期间的可观测性存在缺口；
- 仅检查 Pod Ready 会误判采集链路已经可用。

**根因**：新的采集器没有旧 Promtail 的 positions。首次发现历史 CRI 文件时从 offset 0 回放，而 Loki 默认拒绝超过保留窗口的样本。采集器进程健康与日志写入成功是两个不同的验收层级。

**修复方法**：在 `loki.source.file` 设置 `tail_from_end = true`。该设置只在文件没有既有 position 时从文件尾开始；正常运行后仍由 Alloy 持久化 position 并连续采集新内容。若错误启动已经生成 position，需要清理该次错误迁移产生的 Alloy position，或使用新的空 storage path 后重启一次。

```alloy
loki.source.file "pod_logs" {
  targets       = local.file_match.pod_logs.targets
  tail_from_end = true
  forward_to    = [loki.process.pod_logs.receiver]
}
```

**验收标准**：

1. Alloy 日志在切换后不再出现新的 `timestamp too old` / `final error sending batch`；
2. 主动生成一条带唯一标识的新日志，并在 Loki 中按 namespace/service/标识查询到；
3. 确认时间戳、结构化 metadata 和原始 JSON 内容正确；
4. 上述三项通过后才能删除 Promtail，避免双采或采集空窗。

**参考案例**：2026-07-27 dev K3s 将 Promtail 3.4.2 替换为 Alloy 1.10.2 时，节点存在 14 天前日志，而 Loki 最早只接受 7 天内样本。Alloy 虽 Ready，但真实写入返回 HTTP 400；设置 `tail_from_end` 并重建初始 position 后恢复。

---

## D-018：ConfigMap subPath 挂载后热 reload 仍读取旧 inode

**场景**：应用把 ConfigMap 中的单个文件通过 `subPath` 挂载，例如 Prometheus 的
`/etc/prometheus/prometheus.yml`。修改 ConfigMap 后调用应用热加载端点。

**症状**：ConfigMap 内容已经正确，`POST /-/reload` 也返回成功，但运行时配置和 targets 完全不变。

**根因**：Kubernetes 更新 ConfigMap volume 时切换的是投影目录中的符号链接/inode；`subPath` bind mount
固定在 Pod 创建时的旧 inode，不会跟随切换。应用热加载只是重新读取容器内旧文件，因此无法看到新配置。

**处理与验收**：

1. 先确认 Deployment 使用了 `subPath`，再确认 ConfigMap 新值正确；
2. 执行 `kubectl rollout restart deployment/<name>`，而不是重复调用 reload；
3. 同时从运行时配置 API 和 target API 验证新值；
4. 对需要频繁热更新的配置，优先挂载整个目录，或引入配置 reloader sidecar。

**参考案例**：2026-07-27 dev Prometheus 的抓取 namespace 从错误的 `hci` 修正为 `hci-dev` 后，
`/-/reload` 未生效；滚动重启后 `hci-dev` 业务 targets 从 0 恢复到 9。

---

## D-019：App-of-Apps 双层 selfHeal 覆盖本地联调运行态

**场景**：dev K3s 同时由根 Application 和子 Application 开启 automated selfHeal，本地需要临时部署
尚未进入远端 main 的镜像、Helm 值或 ConfigMap 进行端到端验收。

**症状**：手工修改短暂生效后又自动回滚；只暂停子 Application 后，父 Application 又把子级策略恢复；
Pod 镜像、Prometheus namespace 或观测组件在测试途中漂移。

**根因**：App-of-Apps 中父层管理子 Application 的声明，子层管理业务资源。只暂停一层不能阻止另一层收敛。

**处理与验收**：

1. 明确父子资源所有权，逐层移除 `spec.syncPolicy.automated`；
2. 记录暂停对象、原策略、本地偏差、恢复前置条件和负责人；
3. 验收期间持续检查 Application spec，不能只看一次 UI 状态；
4. 本地改动通过 PR 进入远端并完成同步后，先清除临时运行态，再按原值恢复 selfHeal；
5. 禁止把“暂停自愈”当作长期部署方案。

**参考案例**：Terminal Bridge P0 本地验收同时暂停 `argocd-root`、`hci-platform-dev` 和
`hci-platform-obs-dev`，待业务代码与环境仓值进入远端后才能恢复。

---

## D-020：临时 ConfigMap subPath 覆盖镜像源码，造成“新镜像、旧运行代码”

**触发场景**：为紧急排障直接创建 ConfigMap 并通过 `subPath` 将 Python 源码挂载到容器的
`/app/app/` 或 `/app/shared/` 路径，随后正式镜像虽然升级，运行时仍读取被覆盖的旧文件。

**排查与修复**：先检查 Deployment/StatefulSet/DaemonSet 的 `volumeMounts` 和 managed fields，确认是否存在
`kubectl-patch` 或临时 ConfigMap；删除对应挂载和 ConfigMap，滚动重启并在 Pod 内核验源码版本。禁止继续修改旧
ConfigMap 作为发布方式，正式修复必须构建不可变镜像，经 PR 和 GitOps 发布。

**预防**：通过 `ValidatingAdmissionPolicy` 阻止卷挂载到 `/app/app/`、`/app/shared/` 或 `*.py`，避免运行时源码
覆盖镜像内容。

**参考案例**：2026-07-27 dev 环境运行时代码完整性修复。该类问题会使镜像 tag、探针和 ArgoCD Healthy 状态
与实际执行源码不一致，必须纳入部署验收。
