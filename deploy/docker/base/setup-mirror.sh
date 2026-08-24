#!/bin/sh
# 统一镜像源配置 + Debian 安全升级脚本（全部后端服务 Dockerfile 共用）。
# 各服务 Dockerfile 只 COPY 并调用本脚本，禁止各自写入 apt 镜像 sed。
#
# 行为：
#   MIRROR_MODE=on（默认，本地开发）：
#     - apt 源（deb + security 双 URI）替换为 mirrors.aliyun.com
#     - 写入 /etc/uv/uv.toml 与 /etc/pip.conf，默认索引指向阿里 PyPI
#   MIRROR_MODE=off（CI 构建传入）：
#     - 跳过全部镜像替换，直接使用官方源——GitHub 托管 Runner 在海外，
#       mirrors.aliyun.com 从海外访问高延迟且偶发限流，官方源在 Runner 上更快
#   （两种情况都执行 apt update/upgrade：基础镜像可能晚于 Debian 安全仓库
#     发布，构建时吸收已发布的安全修复。）
#
# 参考：docs/deploy/pitfalls/k8s.md PIT-037（Clash TUN 宿主机构建容器
#       网络不通时，docker build 需加 --network host）。
set -e

if [ "${MIRROR_MODE:-on}" = "on" ]; then
    echo ">>> 启用阿里云镜像源（本地开发路径）..."
    # deb822 格式：/etc/apt/sources.list.d/debian.sources 中 deb 仓库与
    # security 仓库是两个独立 URI 块，必须同时替换，只换 deb.debian.org
    # 会让 security 更新仍走官方源（国内极慢甚至失败）。
    sed -i \
        -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources
    # uv 构建期（pip compile/install）默认索引。
    # 注意：uv.toml 的键是 index-url（default-index 不是合法配置键）。
    mkdir -p /etc/uv
    printf 'index-url = "https://mirrors.aliyun.com/pypi/simple/"\n' > /etc/uv/uv.toml
    # pip 兜底
    printf '[global]\nindex-url = https://mirrors.aliyun.com/pypi/simple/\n' > /etc/pip.conf
else
    echo ">>> MIRROR_MODE=off，使用官方镜像源（CI 海外 Runner 路径）..."
fi

apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
