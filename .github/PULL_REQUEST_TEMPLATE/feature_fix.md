## 变更摘要

- 变更类型：`feat | fix | refactor`
- 影响服务：
- 关联任务/工单：

## 变更范围

- 主要修改文件：
- 涉及共享模块（backend/shared、frontend/shared）：`是/否`
- 涉及数据库或 Schema：`是/否`

## 风险评估

- 风险等级：`低 | 中 | 高`
- 主要风险点：
- 是否影响现有 API 兼容性：`是/否`

## 测试与验证

- [ ] 后端 lint（`uv run ruff check backend/ tests/`）
- [ ] 后端测试（按服务目录运行）
- [ ] 前端 lint/test/build（如有前端改动）
- [ ] Helm 渲染校验（如有部署改动）

关键命令与结果（必填）：

```
# 粘贴测试命令和输出摘要
```

## 发布计划

- 发布策略：`滚动发布 | 灰度 | 金丝雀`
- 目标镜像标签：
- 发布责任人：

## 回滚方案

- 回滚触发条件：
- 回滚方式：`ArgoCD 回滚 | Helm rollback`
- 预计恢复时间（分钟）：

## Reviewer 检查项

- [ ] 变更范围与任务一致，无越界改动
- [ ] 风险评估与回滚方案可执行
- [ ] 测试证据充分，结论可信
- [ ] 不包含敏感信息（密钥、令牌、凭据）

## 静默失效检查（横向改进：每个涉及后端的 PR 必答）

> 静默失效是最难排查的问题类别，请认真逐项确认。

- [ ] 每个 `except Exception` 之前是否有 `except HTTPException: raise` / `except HCIException: raise`？
- [ ] 外部 HTTP 调用（httpx、aiohttp）是否调用了 `response.raise_for_status()`？
- [ ] 后台任务 `asyncio.create_task()` 是否添加了 `add_done_callback` 捕获异常？
- [ ] 业务关键操作失败（如 Pod 释放、KB ingest、DB 写入）是否记录了 WARNING 级别日志？
- [ ] 是否有新增 Prometheus Counter/Gauge 追踪关键失败路径？
- [ ] 共享状态（如 Pod 分配关系）是否在服务重启后能从 Redis 恢复？

## API 变更对齐检查（涉及 backend/shared/models/ 改动时必填）

- [ ] 是否遵循"变更三步法"（先更新共享类型 → 提供方 → 调用方）？
- [ ] 是否保留了旧字段（至少一个 Release 的过渡期）？
- [ ] 契约测试（tests/contract/ 或 tests/integration/test_*_contract.py）是否通过？
