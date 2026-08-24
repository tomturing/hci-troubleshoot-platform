ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

# 镜像源配置与 Debian 安全升级集中维护在 deploy/docker/base/setup-mirror.sh，
# 禁止在本文件单独写入 apt 镜像 sed。本地构建默认阿里镜像（国内网络）；
# CI 构建传入 --build-arg MIRROR_MODE=off 走官方源（GitHub 托管 Runner 在海外）。
ARG MIRROR_MODE=on
COPY deploy/docker/base/setup-mirror.sh /usr/local/bin/setup-mirror.sh
RUN MIRROR_MODE="${MIRROR_MODE}" setup-mirror.sh

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 安装依赖（使用 uv pip，pyproject.toml 为唯一依赖声明源）
COPY backend/api-gateway/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

COPY backend/shared /app/shared
COPY backend/api-gateway /app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
