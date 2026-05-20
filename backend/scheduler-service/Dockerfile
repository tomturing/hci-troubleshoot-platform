FROM python:3.12-slim

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 缓存破坏参数（每次构建时传入不同的值以强制重新 COPY shared）
ARG CACHEBUST=1

WORKDIR /app

# 安装依赖（使用 uv pip，无需虚拟环境）
COPY scheduler-service/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# 使用 CACHEBUST ARG 破坏缓存，确保 COPY shared 总是重新执行
RUN echo "CACHEBUST=${CACHEBUST}" > /tmp/cachebust

COPY shared /app/shared
COPY scheduler-service /app

EXPOSE 8003

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
