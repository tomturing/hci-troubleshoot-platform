---
status: completed
category: deploy
audience: developer, operator
last_updated: 2026-09-15
owner: team
---

# KBD 管道 PostgreSQL 自动端口转发守护

## 事件背景

在本地运行 KBD 数据管道交互 CLI（`uv run python -m data-pipeline.kbd.run cli`）时，流水线在 `pipeline.py:405` 创建 `asyncpg` 连接池处抛出 `ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 5432)`。

经根因分析：
1. 此前 PR #173 及 PR #689 为 `kb-service`（8004 端口）实现了基于 `kubectl port-forward` 的自动探测与守护（`ensure_kb_service_reachable()`），但未涵盖 PostgreSQL 数据库（5432 端口）；
2. 管道在执行任何业务阶段前需要直连数据库进行状态快照查询（断点续跑过滤、`support_id` 与自增 ID 换算）；若开发者未预先手动在终端挂起 `kubectl port-forward svc/postgres-external 5432:5432`，命令将直接崩溃。

## 解决方案

1. **统一隧道管理模块 (`data-pipeline/kbd/tunnel.py`)**：
   - 将原 `importer.py` 中的端口转发管理、PID 元数据、进程防误杀时钟校验（`/proc/<pid>/stat`）与跨进程文件锁收敛为通用模块；
   - 提取通用的 `ensure_service_tunnel` 机制，分别提供 `ensure_kb_service_reachable()` 与 `ensure_postgres_reachable()`；
   - 动态识别命名空间内的 PostgreSQL 服务（优先选择 `svc/postgres-external`，回退至 `svc/postgres`）。

2. **前置守护触发**：
   - 在 `pipeline.py` 的 `_create_pool()` 以及 `_get_failed_vision_ids()` 中，在创建连接池之前自动调用 `ensure_postgres_reachable()`；
   - 在 `extract_signals.py` 与 `image_proc.py` 的独立自建 pool 分支中同样补充守护调用，确保独立运行任一阶段均可自愈。

3. **向后兼容性**：
   - `importer.py` 保留全部原有重导出符号与函数签名，确保已有单元测试与外部调用 100% 兼容。

## 验证结论

- 单元测试：新增 `tests/unit/kbd/test_tunnel.py`，全量回归 `tests/unit/kbd/`（177 passed, 131 skipped）。
- 代码规范：通过 `ruff check` 格式与风格检查。
