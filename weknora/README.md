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

上游 UI 是 vite 构建的 SPA，默认 `base='/'`，
静态资源引用（`/assets/*`）和 axios 的 baseURL（`getApiBaseUrl()` 取
`import.meta.env.BASE_URL`）都指向站点根。而本平台要把 WeKnora 挂在
`/weknora/` 子路径做同源内嵌：

- 同源的好处：iframe 不需要跨域白名单，`embed` 的 CSP `frame-ancestors 'self'`
  直接成立，也不需要给集群再开一个域名或端口。
- v0.9.0 的 `api-base.ts` 已读取 `BASE_URL`（上游为 LocalHub 反代合入的补丁），
  因此**只需用 `vite build --base=/weknora/` 重跑一次前端构建**，
  运行期完全复用官方 `wechatopenai/weknora-ui` 镜像（nginx 配置、
  `/embed/` 的 `auth_request`、`/api` 反代、entrypoint 生成 `config.js` 都不改）。

`ui/Dockerfile` 就是这么做的：`git clone` 上游 tag → `npm ci` → `npm run build`
（`VITE_BASE_PATH=/weknora/`）→ 把 `dist/` 覆盖到官方镜像的
`/usr/share/nginx/html`。**没有 fork 源码，没有运行时补丁。**

## 镜像清单（全部锁版本）

| 组件 | 上游镜像 | 平台地址 |
| --- | --- | --- |
| app | `wechatopenai/weknora-app:v0.9.0` | staging 直拉；离线环境同步到 ghcr |
| docreader | `wechatopenai/weknora-docreader:v0.9.0` | staging 直拉；离线环境同步到 ghcr |
| postgres | `paradedb/paradedb:v0.25.11-pg17` | staging 直拉；离线环境同步到 ghcr |
| redis | `redis:7.0-alpine` | staging 直拉；离线环境同步到 ghcr |
| neo4j | `neo4j:2025.10.1` | staging 直拉；离线环境同步到 ghcr |
| minio | `pgsty/minio:RELEASE.2026-08-04T00-00-00Z` | staging 直拉；离线环境同步到 ghcr |
| ui | 自建（`weknora/ui/Dockerfile`） | CI 构建并推送 ghcr（**单架构，只有 linux/amd64**） |

> 上游 tag 是 `v0.9.0`，**不是** `0.9.0`（后者在 Docker Hub 上不存在，会 404）。

> 自建 UI 镜像由 `.github/workflows/weknora-images.yml` 单平台构建，manifest 里没有 arm64。
> Apple Silicon 本机必须由 Compose 钉 `platform: linux/amd64` 走模拟层（已内置，见下节），
> 而清单里六个上游镜像都有 arm64 变体，**不要**给整栈钉 amd64 —— Postgres/Neo4j 跑模拟层会明显变慢。

`weknora/scripts/sync-upstream-images.sh` 用 `crane copy` 把上游六个镜像搬进私有 registry；
线上入口是 `.github/workflows/weknora-images.yml`（Actions 手动 Run workflow，按版本幂等）。
staging 节点已实测可直拉上述六个上游镜像；自建前缀 UI 仍只能来自 ghcr。
离线/私有客户环境不能依赖外网，部署前必须先运行同步工作流，并在环境仓把 repository
切换为对应的 `weknora-*` 私有副本。

## 部署

- Chart：`deploy/helm/hci-platform-weknora/`（vendored 官方 chart + 平台化）
- 命名空间：`hci-weknora`（ArgoCD `CreateNamespace=true`）
- ArgoCD：
  - dev：`deploy/gitops/argo-apps/local/hci-platform-weknora-dev.yaml`
  - staging：`deploy/gitops/argo-apps/cloud/hci-platform-weknora-staging.yaml`
  - prod：`deploy/gitops/argo-apps/cloud/hci-platform-weknora-prod.yaml`
- 环境参数：独立仓库 `/Users/royin/workspace/sources-sangfor/hci-platform-env` 的
  `environments/<env>/weknora-values.yaml`（dev/staging/prod 各自的环境唯一事实源）
- 流量：**不创建自己的 Ingress**。`/weknora/` 由 admin-ui nginx 反代并剥离前缀，
  与 admin-ui 同源。chart 的 `ingress.enabled=false`。

这是 Argo CD multi-source 双仓部署：本仓只维护 Chart，`deploy/gitops/env-repo-template/`
仅用于新环境初始化和结构参考，**不会被现有 staging Application 直接读取**；镜像地址、
启用开关、容量与环境密钥都必须修改 `hci-platform-env` 的实际 values。

平台 `full` 形态固定为核心五组件 + Neo4j + MinIO，且应用 `STORAGE_TYPE=minio`、
`NEO4J_ENABLE=true`。它不包含上游 Compose 的其他实验性向量库、Dex、SearXNG 或
Langfuse；这些组件若需要，必须另行做容量和安全评审。

### 本地 Docker Compose

```bash
cp deploy/env/weknora.env.example deploy/env/weknora.env
# 替换 change-me 密钥后启动；三个 profile 显式同时激活。
make weknora-up
make weknora-status
```

独立 Compose 加入主平台 external network，服务名 `weknora` 可被 admin-ui 与
api-gateway 直接解析；因此本地管理台仍从 `http://localhost:3002/weknora/` 同源访问。

本地有两个只在 Compose 生效的适配，Helm 部署不受影响：

| 适配 | 原因 |
| --- | --- |
| `platform: ${WEKNORA_UI_PLATFORM:-linux/amd64}` | 自建 UI 镜像只有 amd64；不钉平台时 Compose 在 manifest 解析阶段直接失败，整条 `up` 中止。本地自建 arm64 镜像后把该变量改成 `linux/arm64` |
| `weknora-ui-local-prefix.conf`（挂到 `/etc/nginx/conf.d/`，对外发布 `18080:8081`） | 前缀版镜像的静态资源与 axios baseURL 都带 `/weknora/`，集群里由 admin-ui nginx 剥前缀（`frontend/admin/nginx.conf`）。本地没有那一层，直连会白屏（`/weknora/assets/*.js` 落到 SPA 兜底返回 `index.html`，MIME 变 `text/html`）且 `/weknora/api/*` 不反代。该 server 做同样的 strip 前缀反代，使 `http://localhost:18080/weknora/` 与线上等价；容器自带的 80 仍保留给 admin-ui 链路 |

验收口径（三个都要 200，且 MIME 正确）：

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:18080/weknora/
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:18080/weknora/assets/<hash>.js
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18081/health
```

`.../weknora/assets/*.js` 返回 `200 text/html` 就是前缀没被剥掉（白屏），不是构建产物缺失。

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
2. 跑 `weknora/scripts/sync-upstream-images.sh <新版本>` 同步六个上游镜像。
3. 等 CI 构建出新 UI tag，把它写进 `weknora-values.yaml` 的 `ui.image.tag`。
4. 在 staging 打开 `/weknora/` 与问答面板各走一遍（升级后必须验，不能只看 Pod Running）。

## 构建坑位

v0.9.0 的 `frontend/` 里**没有** `vite.config.ts`，设环境变量 `VITE_BASE_PATH` 完全无效。
必须用 `npm run build -- --base=/weknora/`。若 `--base` 丢失，镜像照样构建成功、
Pod Running、`/weknora/` 返回 200，但页面白屏且控制台报
"Failed to load module script ... MIME type of text/html"。
Dockerfile 已用 `${VITE_BASE_PATH:-/weknora/}` 兜底，不要删。

## 已知边界

- WeKnora 有**独立账号体系**，平台 JWT 不会透传；只有平台管理员能进管理台入口，
  但账号仍要在 WeKnora 内单独登录。
- `VITE_BASE_PATH` 只覆盖 axios 链路。上游少量边缘功能（集成设置里的示例
  `fetch`、沙箱终端 WebSocket）仍用根绝对路径，且沙箱默认关闭
  （`WEKNORA_SANDBOX_DOCKER_ENABLED=false`），不影响知识库主链路。
