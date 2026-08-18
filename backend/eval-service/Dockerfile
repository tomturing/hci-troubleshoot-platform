FROM python:3.12-slim

# 基础镜像可能晚于 Debian 安全仓库发布；构建时吸收已发布的安全修复。
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY backend/eval-service/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

COPY backend/shared /app/shared
COPY backend/eval-service /app

EXPOSE 8007

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007"]
