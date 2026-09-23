# auth-service 镜像：仅做认证（登录/JWT签发/JWKS/审计），不做授权。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend:/app/backend/auth-service

WORKDIR /app/backend/auth-service

COPY backend/auth-service/pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] "sqlalchemy[asyncio]" asyncpg \
    cryptography argon2-cffi pydantic pydantic-settings httpx \
    opentelemetry-api opentelemetry-sdk prometheus-client

COPY backend/ /app/backend/

EXPOSE 8007

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007"]
