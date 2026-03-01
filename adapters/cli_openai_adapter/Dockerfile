FROM python:3.12-slim

WORKDIR /app
COPY adapters/cli_openai_adapter/server.py /app/server.py

ENV PYTHONUNBUFFERED=1 \
    ADAPTER_HOST=0.0.0.0 \
    ADAPTER_PORT=43101

EXPOSE 43101
CMD ["python", "/app/server.py"]
