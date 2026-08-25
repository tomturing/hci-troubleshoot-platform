FROM golang:1.26.6-alpine AS offline-collector-build

WORKDIR /src
COPY backend/diagnosis-service/offline-collector/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -buildvcs=false \
    -trimpath -ldflags="-s -w -buildid=" \
    -o /out/hci-collect-linux-amd64 .

FROM python:3.12-slim

# 镜像源配置与 Debian 安全升级集中维护在 deploy/docker/base/setup-mirror.sh，
# 禁止在本文件单独写入 apt 镜像 sed。本地构建默认阿里镜像（国内网络）；
# CI 构建传入 --build-arg MIRROR_MODE=off 走官方源（GitHub 托管 Runner 在海外）。
ARG MIRROR_MODE=on
COPY deploy/docker/base/setup-mirror.sh /usr/local/bin/setup-mirror.sh
RUN MIRROR_MODE="${MIRROR_MODE}" setup-mirror.sh

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY backend/diagnosis-service/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

COPY backend/shared /app/shared
COPY backend/diagnosis-service/app /app/app
COPY --from=offline-collector-build /out/hci-collect-linux-amd64 /app/resources/bin/hci-collect-linux-amd64

EXPOSE 8008

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]
