# DSH（DeepSeek Harness）Web UI 容器镜像 — 二开源构建
#
# 上游项目：https://github.com/deepseek-ai/deepseek-harness
# 二开仓库：https://github.com/tomturing/deepseek-harness（工单同步 ticket-sync）
#
# 为什么不再直接 npm install 上游发行包：
#   ticket-sync 是 monorepo 内新增的宿主/客户端包，未发布到 npm；
#   `@deepseek-ai/dsh` 的 bin 通过 workspace 依赖加载这些包，
#   只有从二开源整仓构建才能把插件装配进镜像。
#
# 版本锁定：0.2.1-rc.2，二开基线提交 edc3d6e6（PR #1 合并）。
# 升级上游或二开基线时必须同步修改 DSH_REF 并单独评审。
#
# 三条上游契约约束（改动前请先阅读
# docs/solution/events/2026-10-22-DSH诊断集成.md）：
#   1. 监听地址：上游在配置加载期直接拒绝 0.0.0.0 等 unspecified address，
#      因此本镜像把 dsh 固定绑定在 127.0.0.1:3080，只对本 Pod 内的
#      nginx sidecar 开放，对外暴露完全由 sidecar 承担。
#   2. 鉴权：上游无匿名模式，每次进程启动会生成随机的 launch token 并打印
#      形如 `dsh web: https://<host>/dsh/?token=<t>` 的启动行。entrypoint
#      负责抓取该 token 写入 /run/dsh/token，供 sidecar 的
#      /__internal/token 接口读取。
#   3. 就绪信号：该启动行由上游在 Loader 树就绪后才打印，是官方的
#      readiness 信号，因此可作为抓取 token 的同步点。
FROM node:22-alpine AS build

# 构建容器不继承 GitHub Actions runner 的 CI 变量；显式设置让
# root postinstall（install-lefthook.mjs）跳过，避免在 musl 上执行
# glibc 版 lefthook 二进制。
ENV CI=true

# 源码构建需要 git；原生依赖（sharp / node-pty）优先使用 prebuild，
# 补上编译工具链以防 musl 平台没有可用 prebuild 时回退源码编译。
RUN apk add --no-cache git build-base python3 patch linux-headers

ARG DSH_REPO=https://github.com/tomturing/deepseek-harness.git
ARG DSH_REF=edc3d6e61eda06b1cdb053d921c3bb9024e457f3

# 按 commit 浅拉取，避免 master 漂移导致镜像内容不可复现。
RUN git init /src \
    && cd /src \
    && git remote add origin "${DSH_REPO}" \
    && git fetch --depth 1 origin "${DSH_REF}" \
    && git checkout -q FETCH_HEAD

WORKDIR /src

# 安装依赖并构建宿主 + 客户端 + Web 前端。
# 只跑 build:lib / build:web，不跑完整 build，避免打入桌面端/原生发行物。
RUN corepack enable \
    && pnpm install --frozen-lockfile \
    && pnpm run build:lib \
    && pnpm run build:web

# .git 对运行时无用，去掉以缩小镜像。
RUN rm -rf /src/.git

FROM node:22-alpine

# 运行期保留完整 workspace（含 node_modules），保证 workspace 包与
# ticket-sync 的解析关系与构建期一致；镜像体积换部署可靠性。
COPY --from=build /src /opt/dsh

# 提供与 npm 全局安装一致的 `dsh` 命令，entrypoint 无需改动。
RUN ln -s /opt/dsh/apps/cli/lib/bin.js /usr/local/bin/dsh \
    && chmod 0755 /opt/dsh/apps/cli/lib/bin.js

# 预建可写目录；运行时由 emptyDir / PVC 覆盖挂载
RUN mkdir -p /work /home/dsh /run/dsh \
    && chown -R node:node /work /home/dsh /run/dsh /opt/dsh

# 注意：CI 的构建上下文是仓库根目录（matrix.context = "."），
# 不是本文件所在目录，因此路径必须带 dsh/ 前缀。
COPY dsh/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# 固定以非 root 身份运行（node 官方镜像自带 node:node = 1000:1000）
USER 1000:1000

ENV HOME=/home/dsh
ENV DSH_PORT=3080
ENV DSH_PUBLIC_URL=http://127.0.0.1:3080/
ENV DSH_TRUSTED_HOST=localhost

WORKDIR /work

# 3080 仅本 Pod 内可达；对外端口 8080 由 nginx sidecar 承载
EXPOSE 3080

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]