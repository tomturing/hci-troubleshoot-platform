<!-- 💡 按变更类型选择更简洁的模板（在 URL 末尾追加参数）：
     功能/修复：?template=feature_fix.md
     文档更新：?template=docs.md
     杂项/测试：?template=chore.md
     不加参数则使用本完整模板（适用于涉及发布的变更）
-->

## 变更摘要

- 关联任务/工单：
- 变更类型：`feat | fix | refactor | chore | docs | test`
- 影响服务：
- 影响环境：`dev | staging | prod`

## 变更范围

- 主要修改文件：
- 是否涉及共享模块（backend/shared、frontend/shared）：`是/否`
- 是否涉及数据库或 Schema：`是/否`

## 风险评估（必填）

- 风险等级：`低 | 中 | 高`
- 主要风险点：
- 潜在回归点：
- 是否影响现有 API 兼容性：`是/否`

## 测试与验证（必填）

- [ ] 已执行后端 lint（`uv run ruff check backend/ tests/`）
- [ ] 已执行后端测试（按服务/目录）
- [ ] 已执行前端 lint/test/build（如有前端改动）
- [ ] 已执行 Helm 渲染校验（如有部署改动）
- [ ] 已补充或更新必要测试用例

验证证据：

- 关键命令与结果摘要：
- 截图/日志链接（可选）：

## 发布计划（必填）

- 发布窗口：
- 发布策略：`滚动发布 | 灰度 | 金丝雀`
- 目标版本/镜像标签：
- 发布责任人：

## 回滚方案（必填）

- 回滚触发条件：
- 回滚方式：`Argo 回滚 | Helm rollback`
- 回滚目标版本：
- 预计恢复时间（分钟）：

## 可观测性与审计

- [ ] 已确认关键日志包含 trace_id
- [ ] 已确认关键指标可观测（错误率、延迟、资源）
- [ ] 已在发布记录中登记变更信息

## Reviewer 检查项

- [ ] 变更范围与任务一致，无越界改动
- [ ] 风险评估与回滚方案可执行
- [ ] 测试证据充分，结论可信
- [ ] 不包含敏感信息（密钥、令牌、凭据）

## 静默失效检查（涉及后端改动时必答）

- [ ] 每个 `except Exception` 之前是否有 `except HTTPException: raise` / `except HCIException: raise`？
- [ ] 外部 HTTP 调用是否调用了 `response.raise_for_status()`？
- [ ] 后台任务 `asyncio.create_task()` 是否添加了 `add_done_callback` 捕获异常？
- [ ] 业务关键操作失败是否记录了 WARNING 级别日志并上报 Prometheus 指标？

## API 变更对齐（涉及 backend/shared/models/ 改动时必答）

- [ ] 遵循三步法（共享类型 → 提供方 → 调用方）？
- [ ] 保留旧字段过渡期（至少一个 Release）？
- [ ] 契约测试（tests/integration/test_*_contract.py）通过？
