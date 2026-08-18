FROM python:3.12-slim

# 基础镜像可能晚于 Debian 安全仓库发布；构建时吸收已发布的安全修复。
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 安装依赖（使用 uv pip，pyproject.toml 为唯一依赖声明源）
COPY backend/case-service/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

# 复制代码
COPY backend/shared /app/shared
COPY backend/case-service /app

# 暴露端口
EXPOSE 8001

# 启动服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
