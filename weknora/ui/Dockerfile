# WeKnora 管理端 UI — 前缀部署定制镜像
#
# 上游 UI 默认以站点根构建，静态资源与 axios baseURL 都指向根路径，
# 无法直接挂在 /weknora/ 子路径。v0.9.0 的 api-base.ts 已读取
# import.meta.env.BASE_URL（上游为 LocalHub 反向代理合入的补丁），
# 因此用 vite 的 --base 参数重跑一次前端构建即可，运行期仍用官方镜像：
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
    NODE_OPTIONS=--max-old-space-size=4096

# 前缀必须走 vite 原生的 --base CLI 参数，不能靠环境变量：
#   - v0.9.0 的 frontend 目录里**没有** vite.config.ts（base 配置是 main 分支
#     新增的），环境变量不会被读取；
#   - 但 utils/api-base.ts 在 v0.9.0 已读取 import.meta.env.BASE_URL
#     （上游为 LocalHub 反向代理合入的补丁），--base 会同时修正
#     静态资源引用与 axios 的 /api 前缀，两者缺一不可。
# 用 :- 兜底：build-arg 漏传时 vite 会静默回落 base='/'
# ，镜像照样构建成功但页面白屏（模块脚本 MIME 变成 text/html），极难发现。
RUN npm run build -- --base="${VITE_BASE_PATH:-/weknora/}"

# ---------- 运行阶段 ----------
FROM wechatopenai/weknora-ui:${WEKNORA_IMAGE_TAG}

# 仅替换静态产物。运行期仍使用官方镜像的 nginx、entrypoint 与 /etc/nginx/templates。
COPY --from=builder /src/frontend/dist/ /usr/share/nginx/html/
