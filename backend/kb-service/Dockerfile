FROM python:3.12-slim

# 安装 uv
RUN pip install --no-cache-dir uv

WORKDIR /app

# 安装依赖（使用 uv pip，pyproject.toml 为唯一依赖声明源）
COPY backend/kb-service/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache --index-url https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY backend/shared /app/shared
COPY backend/kb-service /app
RUN chmod -R 777 /app/shared
# Helm 的 kbd-pipeline Job 默认复用该镜像；保留仓库目录层级，既让配置模块
# 正确定位项目根目录，也让 `python -m kbd.run` 可通过 PYTHONPATH 启动。
COPY data-pipeline/kbd /app/data-pipeline/kbd
ENV PYTHONPATH="/app/data-pipeline:/app"

EXPOSE 8004

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
