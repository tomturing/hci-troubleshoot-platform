# OpenClaw 避坑

## PIT-010：OpenClaw 返回 401 / 认证 token 不匹配

**现象：** API 请求返回 401，或 scheduler 日志显示 `authentication failed`。

**根因：** `openclaw.json` 中的 `gateway.auth.token` 与 K8s Secret / values 中的 `openclawToken` 不一致。

**排查步骤：**
```bash
# 查看运行时 token 配置
python3 -c "import json; d=json.load(open('/home/node/.openclaw/openclaw.json')); print(d['gateway']['auth'])"

# 查看 K8s Secret 中的 token
sudo k3s kubectl get secret hci-platform-secrets -n hci-troubleshoot -o jsonpath='{.data.openclaw-token}' | base64 -d
```

两者必须完全一致。修改后需 `kubectl rollout restart deployment/openclaw`。

---

## PIT-013：OpenClaw 启动报 JSON Parse Error / non-loopback 绑定失败

**现象：** Pod 持续 CrashLoopBackOff，日志出现：
- `JSON parse error at line 110` 或 `unexpected end of file`
- `Gateway: will not start on non-loopback interface`

**根因：** `/home/node/.openclaw/openclaw.json` 文件被截断（缺少末尾 `}`），配置解析失败，`dangerouslyAllowHostHeaderOriginFallback: true` 未被读取，导致 Gateway 拒绝绑定非 loopback 地址。

**修复：**
```bash
# 检查文件末尾
tail -5 /home/node/.openclaw/openclaw.json

# 如果最后一行不是 }，补上
echo "}" >> /home/node/.openclaw/openclaw.json

# 验证 JSON 完整性
python3 -c "import json; json.load(open('/home/node/.openclaw/openclaw.json'))" && echo "JSON OK"

# 重启 Pod
sudo k3s kubectl rollout restart deployment/openclaw -n hci-troubleshoot
```

**注意：** 该文件是宿主机 HostPath 挂载进 Pod，修改宿主机文件后 Pod 重启即生效，**无需重建镜像**。

**根因通用模式见：** `k8s.md` PIT-018（HostPath 挂载文件被截断，通用排查步骤）

---

## PIT-026：OpenClaw Control UI 报"requires device identity (use HTTPS or localhost)"

**现象：** 通过 HTTP 外网 IP 访问 `/openclaw/` 时，WebUI 报错：
> `Control UI requires device identity (use HTTPS or localhost secure context)`

**根因：** `openclaw.json` 的 `gateway.controlUi` 中缺少 `dangerouslyDisableDeviceAuth: true`。
浏览器在非 HTTPS / 非 localhost 环境下无法完成设备身份标识，OpenClaw 默认拒绝访问。

**修复：**
```bash
# 宿主机直接修改（K3s Pod 使用 hostPath /home/node 挂载，无需进容器）
python3 -c "
import json
path = '/home/node/.openclaw/openclaw.json'
with open(path) as f:
    cfg = json.load(f)
cfg['gateway']['controlUi']['dangerouslyDisableDeviceAuth'] = True
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print('Done')
"

# 重启 Pod 使配置生效
k3s kubectl rollout restart deployment/openclaw -n hci-troubleshoot
k3s kubectl rollout status deployment/openclaw -n hci-troubleshoot
```

**正确的 controlUi 配置：**
```json
"controlUi": {
  "enabled": true,
  "dangerouslyAllowHostHeaderOriginFallback": true,
  "dangerouslyDisableDeviceAuth": true
}
```

**注意：** Docker farm 实例的 `openclaw.json` 由 `/srv/openclaw/data/instance-N/.openclaw/openclaw.json` 管理，
K3s 实例由宿主机 `/home/node/.openclaw/openclaw.json` 管理，两者独立，初始化时都需要设置。

---

## PIT-027：OpenClaw 聊天报 LLM request timed out（Clash TUN 劫持 API 域名）

**现象：** 在 OpenClaw Control UI 发送消息后报错：
> `LLM request timed out.`

Pod 日志出现：
```
[agent/embedded] embedded run agent end: isError=true error=LLM request timed out.
```

**⚠️ 重要：先排查根因类型**

| 根因 | 诊断特征 | 修复方案 |
|------|---------|---------|
| **API 域名本身不可达** | 宿主机 curl 也超时/SSL 失败 | 切换 provider（本条目方案 A/B） |
| **Clash 热重载状态不一致** | 宿主机通、Pod 不通，DNS 结果不一致 | 重启 Clash（见 D-008） |
| **Pod bypass 规则问题** | 宿主机和 Pod DNS 相同 fake-ip，但 Pod 不通 | 配置 Pod DNS（见 PIT-034） |
| **API 余额不足** | HTTP 429 错误 | 充值或更换 API key |

**根因：** `openclaw.json` 配置的模型 provider 为 `zai`，OpenClaw 默认访问 `api.zai.chat`。
该域名被 Clash TUN 劫持解析到 `198.18.0.4`，TLS 握手失败（`SSL_ERROR_SYSCALL`），请求超时。

**排查步骤：**
```bash
# 1. 查看当前使用的模型
python3 -c "import json; c=json.load(open('/home/node/.openclaw/openclaw.json')); print(c['agents']['defaults']['model'])"

# 2. 测试 API 域名连通性
curl -v --max-time 10 https://api.zai.chat/v1/models 2>&1 | tail -5
# 若解析到 198.18.x.x 且 SSL 握手失败 → Clash TUN 劫持，参见 PIT-014

# 3. 测试备用 provider 连通性
curl -o /dev/null -w "HTTP:%{http_code} time:%{time_total}s\n" --max-time 10 \
  https://open.bigmodel.cn/api/paas/v4/models \
  -H "Authorization: Bearer <your-api-key>"
```

**修复方案 A（推荐）：切换到可达的 provider**
```bash
python3 -c "
import json
path = '/home/node/.openclaw/openclaw.json'
with open(path) as f: cfg = json.load(f)
cfg['agents']['defaults']['model'] = {'primary': 'tly/glm-5'}
cfg['agents']['defaults']['models'] = {'tly/glm-5': {'alias': 'GLM-5'}}
with open(path, 'w') as f: json.dump(cfg, f, indent=2, ensure_ascii=False)
print('Done')
"
k3s kubectl rollout restart deployment/openclaw -n hci-troubleshoot
```

**修复方案 B：为 zai provider 显式配置可达的 baseUrl**
在 `openclaw.json` 的 `models.providers.zai` 中加入：
```json
"zai": {
  "baseUrl": "https://直连可用的中转地址/v1",
  "apiKey": "你的-zai-api-key"
}
```

**预防：** 初始化 openclaw.json 时优先使用 `open.bigmodel.cn` 等国内可直连域名的 provider；
避免使用 Clash TUN 会劫持的境外域名，或提前配 NO_PROXY 排除 AI API 域名（参见 PIT-014）。

**参见：** D-008（Clash 热重载导致 fake-ip 映射不一致）、PIT-034（Pod bypass 规则导致 fake-ip 不通）

---

## PIT-030：OpenClaw Control UI 访问空白页（未携带 token）

**现象：** 浏览器访问 `http://<host>:<port>/openclaw` 页面空白，JS/CSS 资源均 200，Pod Running 正常。

**根因：** OpenClaw Control UI 需要在访问时携带 gateway token 进行鉴权，直接访问路径不带 token 时，
UI 加载后无法建立认证连接，页面无内容渲染（空白）。

**正确访问方式：** 需在 URL 中携带 token 参数：
```
http://<host>:<port>/openclaw/?token=<openclaw-gateway-token>
```
token 值见 K8s Secret 或 `.local/values-prod.override.yaml` 中的 `secrets.openclawToken`。

**排查步骤：**
```bash
# 查看当前 token
sudo k3s kubectl get secret hci-secrets -n hci-troubleshoot \
  -o jsonpath='{.data.OPENCLAW_GATEWAY_TOKEN}' | base64 -d && echo
```

**注意：** Pod RESTARTS=0、HTML 和 JS assets 全 200 是迷惑性症状，不要从服务健康角度排查，
直接看浏览器 Network 面板中 WebSocket/API 请求是否有 401/403。

---

## PIT-032：对 WebSocket 服务加 HTTP redirect 导致 WS 断连

**现象：** 添加 Traefik IngressRoute + redirectRegex Middleware 后，openclaw 报"已断开与网关的连接"（1006/1008）。

**根因：** openclaw JS 建立 WebSocket 时连接路径是 `/openclaw`（无斜杠），命中 IngressRoute redirect，
返回 302。**WebSocket 升级请求不遵循 HTTP 302**，直接失败报 1008。
即使让 redirect 携带正确 token 也无济于事。

**结论：** 有 WebSocket 的服务，Ingress 路由只能做代理（proxy），绝对不能加 redirect。

---

## PIT-035：custom-ui 对话报“AI 响应出现错误”，优先容器侧修复（不要先改 Clash）

**现象：** customer/custom-ui 聊天气泡显示：
> `AI 响应出现错误，请稍后重试。`

**容易误判：** 先去改 Clash 全局规则。

**正确原则：** 先排查并修复容器内模型配置与 Pod DNS；只有确认容器侧无问题后才考虑网络全局策略。

**本次根因模型（可复用）：**
- 运行期真正影响 AI 质量的是 Pod 内 `openclaw.json` 与 Pod DNS。
- conversation-service 日志里频繁 `POST /v1/chat/completions 400` 可能是健康探测噪声，不等于用户请求失败。

**排查步骤（不改 Clash）：**
```bash
# 1) 先看真实错误类型（是否 timeout/401/503）
kubectl logs -n hci-troubleshoot deployment/conversation-service --since=2h \
  | grep -iE "conversation_error|ai_exception|timeout|401|503|No available" | tail -50

# 2) 检查容器内模型配置（openclaw + productionclaw 至少各抽样一个）
kubectl exec -n hci-troubleshoot <openclaw-pod> -- sh -lc \
  "node -e 'const fs=require(\"fs\");const c=JSON.parse(fs.readFileSync(\"/home/node/.openclaw/openclaw.json\",\"utf8\"));const p=c?.agents?.defaults?.model?.primary||\"\";const pv=p.split(\"/\")[0]||\"\";console.log(p+\"|\"+(((c.models?.providers||{})[pv]||{}).baseUrl||\"\"));'"

# 期望输出：
# tly/glm-5|https://open.bigmodel.cn/api/paas/v4
```

**修复：**
1. 优先修正容器使用的 `openclaw.json`（模型与 provider baseUrl）并滚动重启对应 Pod。
2. 保持 Pod 级 DNS 方案（见 k8s.md PIT-034），避免 Pod 内解析到不可达 fake-ip。
3. 不要通过“修改 Clash 全局配置”掩盖容器配置问题。

**防回归：**
- `scripts/k3s-verify.sh` 已新增 AI 容器配置检查：
  - openclaw 容器模型必须是 `tly/glm-5`
  - productionclaw 容器模型必须是 `tly/glm-5`
  - baseUrl 必须是 `https://open.bigmodel.cn/api/paas/v4`

---

## OpenClaw 配置快速恢复（迁自 network-service-check.md §九）

**适用场景：** Pod 重启/崩溃后需要快速验证 openclaw.json 配置完整性并恢复服务。

```bash
# 1. 查看 Pod 状态
k3s kubectl get pod -n hci-troubleshoot | grep openclaw

# 2. 配置完整性检查
python3 -c "
import json
c = json.load(open('/home/node/.openclaw/openclaw.json'))
g = c['gateway']
print('bind:', g.get('bind'))
print('token:', g['auth'].get('token'))
print('controlUi:', g.get('controlUi'))
print('model:', c['agents']['defaults']['model'])
"

# 3. 重启
k3s kubectl rollout restart deployment/openclaw -n hci-troubleshoot
k3s kubectl rollout status  deployment/openclaw -n hci-troubleshoot
```

**必须存在的配置项（缺任何一项都会出错）：**
```json
{
  "gateway": {
    "bind": "lan",
    "auth": { "mode": "token", "token": "aihci-openclaw-token" },
    "controlUi": {
      "enabled": true,
      "dangerouslyAllowHostHeaderOriginFallback": true,
      "dangerouslyDisableDeviceAuth": true
    }
  },
  "agents": { "defaults": { "model": { "primary": "tly/glm-5" } } }
}
```
