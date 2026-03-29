# 网络与服务异常排查专项指南

> **第一原则：先用快速全景检查定位层级，再按层级深入。不要跳过这一步。**
> 触发场景：任何"访问不了 / 超时 / 502 / 503 / SSL错误 / 页面不对"类问题。

---

## 一、快速全景检查（60 秒定位层级）

```bash
# 1. 外网入口
curl -o /dev/null -w "HTTP:%{http_code} time:%{time_total}s\n" --max-time 5 http://14.17.59.69:4888/

# 2. 本机入口（排除外网/防火墙）
curl -o /dev/null -w "HTTP:%{http_code} time:%{time_total}s\n" --max-time 5 http://127.0.0.1:4888/

# 3. 核心端点逐一验证
curl -sI http://127.0.0.1:4888/           | head -2   # customer-ui
curl -sI http://127.0.0.1:4888/admin/     | head -2   # admin-ui
curl -s  http://127.0.0.1:4888/api/cases/ | head -1   # api-gateway
curl -s  http://127.0.0.1:4888/grafana/api/health      # grafana
curl -sI http://127.0.0.1:4888/openclaw/  | head -2   # openclaw

# 4. Pod 状态
k3s kubectl get pod -n hci-troubleshoot
k3s kubectl get pod -n hci-observability

# 5. Docker farm
docker ps --format "{{.Names}} {{.Status}}" | grep openclaw

# 6. DNS 是否被 Clash 劫持（看是否解析到 198.18.x.x）
nslookup api.zai.chat && nslookup open.bigmodel.cn
```

**快速定位矩阵：**

| 现象 | 优先排查 |
|------|---------|
| ①挂 ②通 | 外网防火墙/NAT/端口，见 §二 |
| ①②都挂 | Traefik/K3s，见 §三 |
| HTTP 502/503 | 后端 Pod，见 §四 |
| HTTP 200 但内容不对 | nginx/缓存，见 §五 |
| LLM 超时 / SSL Error | AI API + Clash TUN，见 §六 |
| Pod ImagePullBackOff | 镜像未导入 K3s，见 §七 |

---

## 二、Clash TUN 劫持问题（高频！）

> **本节是 Clash TUN 问题的知识枢纽。** 各专项坑点分散在多个文件，此处统一索引。

**Clash TUN 影响全表（所有相关 PIT）：**

| 受影响范围 | 现象 | 详细坑点 |
|-----------|------|---------|
| K3s ClusterIP (10.43.x.x) | Pod 间 Service 调用断路 | `k8s.md` PIT-014 |
| Docker 网段 172.16.0.0/12 | 容器端口映射外网 ERR_EMPTY_RESPONSE | `k8s.md` PIT-038 |
| K3s Pod 出网（fake-ip） | Pod 内访问外部 AI API 超时 | `k8s.md` PIT-034 |
| Docker build 构建容器 | npm/apt/pip install 超时 (198.18.x.x) | `frontend.md` PIT-028 / `k8s.md` PIT-037 |
| OpenClaw LLM API 域名 | LLM request timed out（api.zai.chat） | `openclaw.md` PIT-027 |

**判断是否为 Clash 劫持：**
```bash
nslookup <域名>
# 如果解析到 198.18.x.x → 被 Clash TUN 劫持
curl -v --max-time 5 https://<域名> 2>&1 | grep -E "198.18|SSL_ERROR|Connected"
```

**本项目 Clash TUN 已知影响全表：**

| 域名/网段 | 影响 | 修复 |
|----------|------|------|
| K3s ClusterIP (10.43.x.x) | Pod 互访断路 | 关闭 TUN 或配 bypass，见 k8s.md PIT-014 |
| Docker 172.16/172.12 | 容器互访断路 | 见 k8s.md PIT-038 |
| api.zai.chat | OpenClaw LLM 超时 | 切换 provider 到 open.bigmodel.cn，见 openclaw.md PIT-027 |
| registry.npmjs.org | Docker build npm install 超时 | `docker build --network host`，见 frontend.md PIT-028 |
| registry.npmmirror.com | 同上（国内镜像也被劫持） | 同上 |
| Traefik 80/443 | 端口被占用 | 见 k8s.md PIT-021 |

**Docker build 被劫持的特征：**
```
npm error: connect ETIMEDOUT 198.18.0.19:443
```
**修复：** `docker build --network host ...`（让构建容器使用宿主机网络，走 Clash 代理）

---

### PIT-039：CoreDNS hosts 插件冲突（Pod 解析 GitHub 失败）

**现象：**
- CoreDNS CrashLoopBackOff，日志：`plugin/hosts: this plugin can only be used once per Server Block`
- 所有 Pod DNS 解析失败（admin-ui、customer-ui 找不到 api-gateway）
- WSL 重启后出现，之前运行正常

**根因：**
CoreDNS 的 `hosts` 插件在每个 Server Block（监听块）只能出现一次。如果通过 `import` 引入了额外的 hosts 配置，会导致冲突。

错误示例（会崩溃）：
```yaml
# 主 Corefile
.:53 {
    hosts /etc/coredns/NodeHosts { ... }   # 第一个 hosts
    import /etc/coredns/custom/*.override   # 引入下面的配置
}

# coredns-custom (custom.override)
hosts github-hosts { ... }   # 第二个 hosts → 冲突！
```

**为什么 WSL 重启前没问题？**
CoreDNS 的 `reload` 插件检测配置变化时，如果新配置有冲突，reload 会失败但**不会杀死旧进程**。旧的 CoreDNS 继续用旧配置运行。WSL 重启后 Pod 必须重新加载完整配置，才暴露冲突。

**正确修复方法（环境层面，不动代码）：**

修改 Clash 配置的 `fake-ip-filter`，让 GitHub 相关域名走真实 DNS：

```yaml
# Clash 配置文件（config.yaml）
fake-ip-filter:
  # GitHub 相关域名 — 获取真实 IP，绕过 fake-IP
  - '+.github.com'
  - '+.githubusercontent.com'
  - '+.githubassets.com'
  - 'github.com'
  - '*.github.com'
  - '*.githubusercontent.com'
  - '*.githubassets.com'
  # 其他需要真实 IP 的域名（按需添加）
  - 'localhost'
  - '*.localhost'
  - '+.local'
```

**原理：** Clash TUN 模式会把 DNS 查询返回 fake-IP（198.18.x.x），导致 Pod 内访问 GitHub 时连接失败。配置 `fake-ip-filter` 后，这些域名会走真实 DNS 解析，得到 GitHub 真实 IP。

**验证修复：**
```bash
# 重启 Clash TUN 后验证
nslookup github.com
# 应返回真实 IP（如 140.82.112.4），而不是 198.18.x.x

# 从 Pod 内验证
kubectl exec -n hci-dev <pod-name> -- nslookup github.com
```

---

## 三、K3s / Traefik 排查

```bash
# Traefik 日志
k3s kubectl logs -n kube-system deployment/traefik --tail=30 | grep -iE "error|refused|no route"

# Ingress 规则
k3s kubectl get ingress -n hci-troubleshoot -o wide
k3s kubectl get ingress -n hci-observability -o wide
k3s kubectl describe ingress -n hci-troubleshoot

# Service 端点链路（无 Endpoints 说明 Pod 没就绪）
k3s kubectl get endpoints -n hci-troubleshoot
```

**高频坑：**
- Traefik 占用 80/443 → 修改监听端口，见 k8s.md PIT-021
- Ingress 跨命名空间 Service 不可达 → Ingress 必须与 Service 同命名空间，见 k8s.md PIT-024
- Grafana Ingress 只有域名规则，IP 访问返回空 → 见 grafana.md PIT-012

---

## 四、后端 Pod 排查

```bash
# Pod 概览（关注 Error/CrashLoop/ImagePullBackOff）
k3s kubectl get pod -n hci-troubleshoot -o wide

# 查日志
k3s kubectl logs -n hci-troubleshoot <pod-name> --tail=50

# Pod 事件（看 OOMKilled/Liveness 失败等）
k3s kubectl describe pod -n hci-troubleshoot <pod-name> | tail -20

# 数据库连接
k3s kubectl exec -n hci-troubleshoot postgres-0 -- pg_isready -U postgres
k3s kubectl exec -n hci-troubleshoot redis-0   -- redis-cli ping
```

**高频坑：**
- DATABASE_URL 密码含 `@` 符号 → URL encode 为 `%40`，见 k8s.md PIT-022
- Pod ImagePullBackOff → 镜像未导入，见 §七

---

## 五、nginx / 页面内容排查

```bash
# 查响应头（最先看 Cache-Control）
curl -sI http://127.0.0.1:4888/admin/
curl -sI http://127.0.0.1:4888/

# nginx no-cache 是否还在（Pod 重启后 hot-patch 会丢失！）
k3s kubectl exec -n hci-troubleshoot <admin-ui-pod> -- grep "no-store" /etc/nginx/conf.d/default.conf

# nginx 错误日志
k3s kubectl exec -n hci-troubleshoot <admin-ui-pod> -- cat /var/log/nginx/error.log | tail -10
```

**高频坑：**
- 页面内容不更新 → 浏览器缓存（Ctrl+Shift+R 强刷），见 frontend.md PIT-025
- nginx hot-patch 重启丢失 → 需构建固化镜像，见 §八

---

## 六、AI API 连通性排查

```bash
# 快速测试两个 provider
curl -o /dev/null -w "bigmodel: HTTP:%{http_code} %.2fs\n" --max-time 8 \
  https://open.bigmodel.cn/api/paas/v4/models \
  -H "Authorization: Bearer 489c764be9644feb9a3c91b73c5698e9.rdZeTEGy0XOZa1DM"

curl -v --max-time 5 https://api.zai.chat 2>&1 | grep -E "198.18|SSL_ERROR|http_code"

# 当前 OpenClaw 使用的模型
python3 -c "import json; c=json.load(open('/home/node/.openclaw/openclaw.json')); print(c['agents']['defaults']['model'])"

# OpenClaw 日志
k3s kubectl logs -n hci-troubleshoot deployment/openclaw --tail=20 | grep -E "timeout|error|model"
```

**已验证可用：** `tly/glm-5` → `open.bigmodel.cn`（HTTP 200，0.25s）
**已验证不可用：** `zai/glm-4.7` → `api.zai.chat`（Clash TUN 劫持，SSL 失败）

---

## 七、镜像 ImagePullBackOff 排查

```bash
# 描述 Pod 看错误
k3s kubectl describe pod -n hci-troubleshoot <pod-name> | grep -A3 "Warning\|Failed"

# 检查 K3s containerd 中是否存在对应镜像
sudo k3s ctr images list | grep <image-name>

# 导入本地镜像（必须用 sudo，或先 docker save 再 sudo ctr import）
docker save <image>:<tag> -o /tmp/img.tar
sudo k3s ctr images import /tmp/img.tar

# 或通过管道（sudo 有密码时管道方式可能失败，推荐文件方式）
docker save <image>:<tag> | sudo k3s ctr images import -
```

**注意：K3s 不读取 Docker daemon 的镜像，必须单独导入 containerd。**

---

## 八、nginx hot-patch 丢失后应急

**场景：** Pod 重启后 `Cache-Control: no-store` 头消失。

方式 A（临时，下次重启仍丢失）：
```bash
POD=$(k3s kubectl get pod -n hci-troubleshoot -l app=admin-ui -o name | head -1 | cut -d/ -f2)
k3s kubectl cp frontend/admin/nginx.conf hci-troubleshoot/${POD}:/etc/nginx/conf.d/default.conf
k3s kubectl exec -n hci-troubleshoot ${POD} -- nginx -s reload
```

方式 B（永久固化，推荐）：
```bash
cd /aihci/hci-troubleshoot-platform
docker build --network host -t hci-admin-ui:<new-tag>   -f frontend/admin/Dockerfile    frontend/
docker build --network host -t hci-customer-ui:<new-tag> -f frontend/customer/Dockerfile frontend/
docker save hci-admin-ui:<new-tag>   -o /tmp/admin.tar && sudo k3s ctr images import /tmp/admin.tar
docker save hci-customer-ui:<new-tag> -o /tmp/customer.tar && sudo k3s ctr images import /tmp/customer.tar
helm upgrade hci-platform deploy/helm/hci-platform \
  --reuse-values \
  --set adminUI.image.tag=<new-tag> \
  --set customerUI.image.tag=<new-tag> \
  -n hci-troubleshoot
```

**build 命令必须加 `--network host`**，否则 npm install 被 Clash TUN 劫持超时。

---

## 九、OpenClaw 快速恢复

> 本节内容已迁移到 `openclaw.md` 末尾 **“OpenClaw 配置快速恢复”** 节，请在那里查阅。
包含：python3 配置完整性检查、必须存在的 JSON 配置项、kubectl rollout 重启步骤。

---

## 十、生产环境重启后完整验证清单

```bash
# 1. 所有 Pod Running
k3s kubectl get pod -n hci-troubleshoot
k3s kubectl get pod -n hci-observability
```

---

## 十一、工单创建 500：`case.close_reason` 字段缺失

> 本节内容已迁移到 `debugging.md` **原则六**（K8s ConfigMap subPath + 应用层 schema 漂移）范畺，请在那里查阅。
包含：现象描述、根因分析、快速修复命令、验证步骤、预防措施。
