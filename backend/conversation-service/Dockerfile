FROM python:3.12-slim

WORKDIR /app

COPY conversation-service/requirements.txt .
RUN pip install --no-cache-dir setuptools && pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY conversation-service /app

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
