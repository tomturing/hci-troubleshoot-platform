FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG CACHEBUST=1

WORKDIR /app

COPY eval-service/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

COPY shared /app/shared
COPY eval-service /app

EXPOSE 8007

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007"]
