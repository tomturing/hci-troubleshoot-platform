FROM python:3.12-slim

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 安装依赖（使用 uv pip，pyproject.toml 为唯一依赖声明源）
COPY backend/agent-service/pyproject.toml .
RUN uv pip compile pyproject.toml --no-annotate --no-header -q -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

COPY backend/shared /app/shared
COPY backend/agent-service /app

# 修正代码目录权限：构建不应依赖宿主机 umask。否则 COPY 会保留宿主机的 600 权限，
# 运行时 securityContext（runAsUser=1000, runAsNonRoot=true）的非 root 用户无法读取
# 源码，导致 uvicorn 在 import_from_string 阶段 PermissionError 崩溃。
RUN chmod -R u+rwX,g+rX,o+rX /app

# Shared Resolution 必须能在最终镜像布局中读取 aCLI Catalog。缺失时在构建
# 阶段失败，避免所有 qfk_system 到运行时才被误判为未知命令。
RUN python -c "from shared.resolution.catalog import load_acli_catalog, command_path_known; assert load_acli_catalog() and command_path_known(['acli', 'system', 'ps'])"

EXPOSE 8005

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8005"]
