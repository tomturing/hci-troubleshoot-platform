FROM python:3.12-slim

WORKDIR /app

# 安装依赖
# 安装依赖
COPY case-service/requirements.txt .
RUN pip install --no-cache-dir setuptools && pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY shared /app/shared
COPY case-service /app

# 暴露端口
EXPOSE 8001

# 启动服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
