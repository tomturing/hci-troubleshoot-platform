FROM python:3.12-slim

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 安装依赖（使用 uv pip，无需虚拟环境）
COPY kb-service/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY shared /app/shared
COPY kb-service /app

EXPOSE 8004

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
