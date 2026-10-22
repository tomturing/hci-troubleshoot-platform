# DSH（DeepSeek Harness）Web UI 容器镜像
#
# 上游项目：https://github.com/deepseek-ai/deepseek-harness
# 版本锁定：0.2.1-rc.2。上游处于 developer preview 且明确声明会有破坏性变更，
# 升级版本必须单独评审，不可跟随 latest。
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
FROM node:22-alpine

ARG DSH_VERSION=0.2.1-rc.2

# 上游为纯 ESM 包且无 install 脚本，不需要 build-base / python3 等原生编译工具链
RUN npm install -g "@deepseek-ai/dsh@${DSH_VERSION}" \
    && npm cache clean --force

# 预建可写目录；运行时由 emptyDir / PVC 覆盖挂载
RUN mkdir -p /work /home/dsh /run/dsh \
    && chown -R node:node /work /home/dsh /run/dsh

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
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
