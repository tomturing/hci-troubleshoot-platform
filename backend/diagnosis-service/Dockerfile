FROM golang:1.26.5-alpine AS offline-collector-build

WORKDIR /src
COPY backend/diagnosis-service/offline-collector/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -buildvcs=false \
    -trimpath -ldflags="-s -w -buildid=" \
    -o /out/hci-collect-linux-amd64 .

FROM python:3.12-slim

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
