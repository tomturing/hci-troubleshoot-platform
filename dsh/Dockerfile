# DSH（DeepSeek Harness）Web UI 容器镜像 — 二开源构建
#
# 上游项目：https://github.com/deepseek-ai/deepseek-harness
# 二开改动：deploy/dsh-fork/patches/0001-ticket-sync-and-ui-entry.patch
#
# 为什么按 patch 构建而不是直接拉二开 fork：
#   fork 的 master 会漂移，镜像内容无法从本仓库复现；patch 与上游 tag 一一对应，
#   评审时能在本仓库看到全部差异。代价是 patch 必须包含**所有**文件（含
#   tsconfig 项目引用），否则构建会通过 apply 检查却在类型解析阶段失败。
#
# 版本锁定：上游 tag 0.2.1-rc.2。升级上游时必须重新生成 patch 并单独评审。
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
ARG DSH_VERSION=0.2.1-rc.2

# ── 阶段一：从上游 tag + 本地 patch 构建 ──────────────────────────────────
FROM node:22-alpine AS build

# git：拉取上游源码；build-base/python3：原生依赖（sharp / node-pty）在 musl
# 平台没有 prebuild 时回退源码编译；bash：部分构建脚本显式声明 #!/bin/bash。
RUN apk add --no-cache git python3 make g++ bash \
    && corepack enable

# 构建容器不继承 GitHub Actions runner 的 CI 变量；显式设置让
# root postinstall（install-lefthook.mjs）跳过，避免在 musl 上执行
# glibc 版 lefthook 二进制。
ENV CI=true

WORKDIR /src

# ARG 在 FROM 之前声明只对 `FROM` 行可见，进入构建阶段必须重新声明一次；
# 否则 `${DSH_VERSION}` 展开为空，命令变成 `--branch v`（CI 实际踩过）。
ARG DSH_VERSION

# 上游 tag 带 `dsh-` 前缀（实测 refs/tags/dsh-v0.2.1-rc.2），不是裸 `v0.2.1-rc.2`。
# patch 相对该 tag 生成，版本必须与 ARG 一致，否则 apply 失败（这是期望的失败）。
RUN git clone --depth 1 --branch "dsh-v${DSH_VERSION}" https://github.com/deepseek-ai/deepseek-harness .

COPY deploy/dsh-fork/patches/ /patches/
RUN git apply /patches/0001-ticket-sync-and-ui-entry.patch

# 用 --no-frozen-lockfile：patch 会同时修改 pnpm-lock.yaml，锁文件与上游 tag
# 不是同一份；以 package.json 为准重算，保证新增的两个 workspace 包能被解析。
RUN pnpm install --no-frozen-lockfile

# 只跑宿主 + 客户端 + Web 前端的构建，不跑桌面端 / 原生发行物。
# 顺序不可换：build:lib 里 tsc -b 先产出 typert 的 remote 类型，build:web 依赖它。
RUN pnpm run build:lib \
    && pnpm run build:web

# .git 对运行时无用，去掉以缩小镜像。
RUN rm -rf /src/.git

# ── 阶段二：运行时镜像 ────────────────────────────────────────────────────
FROM node:22-alpine

# unzip 是硬依赖：工单包解密解压全部走它；alpine 基础镜像只带 busybox 的
# tar/gzip，没有 unzip（缺它时每个任务都会以「容器内缺少命令」失败）。
# 刻意不装 coreutils：容量与目录遍历已改用 Node 的 fs.statfs / fs.readdir，
# 不再依赖 df/du/find 的 GNU 方言。
RUN apk add --no-cache unzip

# 运行期保留完整构建产物（含 node_modules 与 workspace 包），保证 Loader 对
# ticket-sync 的解析关系与构建期完全一致；镜像体积换部署可靠性。
COPY --from=build /src /opt/dsh

# 提供与 npm 全局安装一致的 `dsh` 命令，entrypoint 无需改动。
RUN ln -s /opt/dsh/apps/cli/lib/bin.js /usr/local/bin/dsh \
    && chmod 0755 /opt/dsh/apps/cli/lib/bin.js

# alpine 无 openssh / rsync；仅在 tickets.syncer=ssh（HTTP 端口不被允许时的退路）时需要。
ARG INSTALL_SSH_SYNC=false
RUN if [ "$INSTALL_SSH_SYNC" = "true" ]; then \
      apk add --no-cache openssh-client rsync; \
    fi

# 预建可写目录；运行时由 emptyDir / hostPath 覆盖挂载
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
