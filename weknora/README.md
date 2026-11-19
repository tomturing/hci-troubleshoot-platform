# WeKnora 知识库接入

把 [Tencent/WeKnora](https://github.com/Tencent/WeKnora)（MIT）作为平台的知识库能力，
以 `/weknora/` 子路径同源内嵌进 admin-ui，提供两个入口：

| 入口 | 路径 | 说明 |
| --- | --- | --- |
| 知识问答 | `/weknora/embed/<channelId>?token=<ems_...>` | 官方 embed 渠道，访客免登录，只能检索问答 |
| 知识库管理 | `/weknora/` | WeKnora 完整控制台，需要 WeKnora 自己的账号 |

admin-ui 侧对应页面：`frontend/admin/src/views/KnowledgeBaseView.vue`，
后端下发入口：`GET /api/weknora/launch`。

## 为什么需要自建 UI 镜像

上游 UI 是 vite 构建的 SPA，`vite.config.ts` 默认 `base='/'`，
静态资源引用（`/assets/*`）和 axios 的 baseURL（`getApiBaseUrl()` 取
`import.meta.env.BASE_URL`）都固定在站点根。而本平台要把 WeKnora 挂在
`/weknora/` 子路径做同源内嵌：

- 同源的好处：iframe 不需要跨域白名单，`embed` 的 CSP `frame-ancestors 'self'`
  直接成立，也不需要给集群再开一个域名或端口。
- 上游已经提供了构建开关 `VITE_BASE_PATH`，因此**只需重跑一次前端构建**，
  运行期完全复用官方 `wechatopenai/weknora-ui` 镜像（nginx 配置、
  `/embed/` 的 `auth_request`、`/api` 反代、entrypoint 生成 `config.js` 都不改）。

`ui/Dockerfile` 就是这么做的：`git clone` 上游 tag → `npm ci` → `npm run build`
（`VITE_BASE_PATH=/weknora/`）→ 把 `dist/` 覆盖到官方镜像的
`/usr/share/nginx/html`。**没有 fork 源码，没有运行时补丁。**

## 镜像清单（全部锁版本）

| 组件 | 上游镜像 | 平台地址 |
| --- | --- | --- |
| app | `wechatopenai/weknora-app:v0.9.0` | 同步到 ghcr（见下） |
| docreader | `wechatopenai/weknora-docreader:v0.9.0` | 同步到 ghcr |
| postgres | `paradedb/paradedb:v0.25.11-pg17` | 同步到 ghcr |
| redis | `redis:7.0-alpine` | 同步到 ghcr |
| ui | 自建（`weknora/ui/Dockerfile`） | CI 构建并推送 ghcr |

> 上游 tag 是 `v0.9.0`，**不是** `0.9.0`（后者在 Docker Hub 上不存在，会 404）。

`weknora/scripts/sync-upstream-images.sh` 用 `crane copy` 把上游四个镜像搬进私有 registry；
线上入口是 `.github/workflows/weknora-images.yml`（Actions 手动 Run workflow，按版本幂等）。
集群节点的 Docker Hub 加速器对第三方 namespace 返回 403，因此**不能**依赖直连
Docker Hub，镜像必须先同步。

## 部署

- Chart：`deploy/helm/hci-platform-weknora/`（vendored 官方 chart + 平台化）
- 命名空间：`hci-weknora`（ArgoCD `CreateNamespace=true`）
- ArgoCD：`deploy/gitops/argo-apps/cloud/hci-platform-weknora-staging.yaml`
- 环境参数：`hci-platform-env` 的 `environments/<env>/weknora-values.yaml`
- 流量：**不创建自己的 Ingress**。`/weknora/` 由 admin-ui nginx 反代并剥离前缀，
  与 admin-ui 同源。chart 的 `ingress.enabled=false`。

## 首次接入步骤（人工一次性）

1. 部署完成后，从 `kubectl -n hci-weknora get secret weknora-secrets` 取管理员初始
   账号口径（见 chart `secrets` 段），登录 `/weknora/` 完成初始化。
2. 在 WeKnora「设置 → 模型」里配置 LLM 与 Embedding（必须两者都有，否则文档无法
   建索引）。当前 staging 复用平台内部 OpenAI 兼容网关 `it-oneapi.sangfor.com:33380`。
3. 建知识库 → 上传文档 → 建一个 Agent（绑定该知识库）。
4. 「设置 → 网页嵌入」新建渠道，绑定 Agent，**允许域名填 admin-ui 的实际 origin**
   （例如 `https://acli.sangfor.com.cn:4443`）。拿到 `channel_id` 与 `em_` publish token。
5. 把这两个值写进 `hci-platform-env` 的 `weknora-values.yaml`
   （`embed.channelId` / `embed.publishToken`），ArgoCD 同步后
   `/api/weknora/launch` 即可下发带 `ems_` 短令牌的问答入口。

> publish token 只落在集群 Secret 里，网关用它在服务端换取 30 分钟的 `ems_`
> 短令牌，浏览器拿到的永远不是长期令牌。

## 升级 SOP

1. 改 `weknora/ui/Dockerfile` 的 `ARG WEKNORA_VERSION` 与 `WEKNORA_IMAGE_TAG`。
2. 跑 `weknora/scripts/sync-upstream-images.sh <新版本>` 同步四个上游镜像。
3. 等 CI 构建出新 UI tag，把它写进 `weknora-values.yaml` 的 `ui.image.tag`。
4. 在 staging 打开 `/weknora/` 与问答面板各走一遍（升级后必须验，不能只看 Pod Running）。

## 已知边界

- WeKnora 有**独立账号体系**，平台 JWT 不会透传；只有平台管理员能进管理台入口，
  但账号仍要在 WeKnora 内单独登录。
- `VITE_BASE_PATH` 只覆盖 axios 链路。上游少量边缘功能（集成设置里的示例
  `fetch`、沙箱终端 WebSocket）仍用根绝对路径，且沙箱默认关闭
  （`WEKNORA_SANDBOX_DOCKER_ENABLED=false`），不影响知识库主链路。
