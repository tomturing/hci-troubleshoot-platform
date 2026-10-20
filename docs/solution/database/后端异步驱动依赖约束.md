# 后端服务依赖：SQLAlchemy 异步驱动（greenlet）

## 硬性约束

所有在运行时通过 `backend/shared/database/postgres.py` 使用 `sqlalchemy.ext.asyncio`
（即 `create_async_engine` / `AsyncSession`）的后端服务，其 `pyproject.toml` **必须**声明
`sqlalchemy[asyncio]`（而不是裸 `sqlalchemy`），或显式列出 `greenlet`。

- 裸 `sqlalchemy` 包**不会**拉取 `greenlet`；`greenlet` 仅作为 `[asyncio]` extra 的依赖存在。
- 镜像构建使用 `uv pip compile pyproject.toml`（`backend/*/Dockerfile`），**不读取提交的
  `uv.lock`**，依赖解析完全由 `pyproject.toml` 决定。若未声明 `[asyncio]`，重建镜像会缺
  `greenlet`，容器启动即 `ImportError: The SQLAlchemy asyncio module requires that the
  Python 'greenlet' library is installed`，表现为 CrashLoopBackOff。
- 受影响服务（均 import `backend/shared/database/postgres.py`）：agent / api-gateway / case /
  conversation / diagnosis / eval / kb。`auth-service` 已正确声明 `sqlalchemy[asyncio]`，
  可作为范式参考。

## 修改依赖时的注意事项

- 改动任一 `backend/*/pyproject.toml` 的 sqlalchemy 声明后，须同步重算 `uv.lock`
  （根与 7 个服务各一份，执行 `uv lock`），否则本地 `uv sync --locked` 会因锁不一致失败。
- 镜像构建忽略 `uv.lock`，以 `pyproject.toml` 为唯一事实源；`uv.lock` 仅用于 CI `uv sync` 测试。
- `agent-service` 曾出现 `ImagePullBackOff`（registry 镜像缺失，属独立 infra 问题），与
  greenlet 缺失无直接关系；补 `[asyncio]` 只保证其未来重建后含 greenlet。
