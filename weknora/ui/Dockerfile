# WeKnora 管理端 UI — 前缀部署定制镜像
#
# 上游 UI 用 vite 默认 base='/' 构建，静态资源与 axios baseURL 都固定在站点根，
# 无法直接挂在 /weknora/ 子路径。上游因此暴露了 VITE_BASE_PATH 构建开关
# （frontend/vite.config.ts: base: process.env.VITE_BASE_PATH || '/'），
# 本 Dockerfile 只重跑一次前端构建，运行期仍用官方镜像：
#   - nginx 配置（/embed/ 的 auth_request + CSP、/api 反代）不动
#   - entrypoint 仍会生成 config.js 并 envsubst 模板
# 维护面因此收敛到「只换 /usr/share/nginx/html 里的 dist」。
#
# 升级 = 同时改 WEKNORA_VERSION（源码 tag）与 WEKNORA_IMAGE_TAG（运行期镜像 tag），
# 两者必须一致；Docker Hub 上的 tag 是 v0.9.0，不是 0.9.0。

ARG WEKNORA_IMAGE_TAG=v0.9.0

# ---------- 构建阶段 ----------
FROM node:24-bookworm-slim AS builder

ARG WEKNORA_VERSION=v0.9.0
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG VITE_BASE_PATH=/weknora/

# 只用 git 拉上游源码；构建上下文是仓库根，不依赖本仓库任何文件。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${WEKNORA_VERSION}" \
        https://github.com/Tencent/WeKnora.git /src

WORKDIR /src/frontend

# lockfile 固定了远程 SheetJS 归档及其完整性哈希，必须用 npm ci。
RUN npm ci --registry="${NPM_REGISTRY}"

ENV VITE_IS_DOCKER=true \
    VITE_BASE_PATH="${VITE_BASE_PATH}" \
    NODE_OPTIONS=--max-old-space-size=4096

RUN npm run build

# ---------- 运行阶段 ----------
FROM wechatopenai/weknora-ui:${WEKNORA_IMAGE_TAG}

# 仅替换静态产物。运行期仍使用官方镜像的 nginx、entrypoint 与 /etc/nginx/templates。
COPY --from=builder /src/frontend/dist/ /usr/share/nginx/html/
