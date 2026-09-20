---
status: active
category: task
audience: developer
last_updated: 2026-09-20
owner: 平台研发
---

# terminal_bridge 日志治理与上传补采任务

## 关联方案

[方案文档](../../solution/events/2026-09-20-terminal_bridge日志治理与上传补采方案.md) ·
[需求文档](../../requirement/events/2026-09-20-terminal_bridge日志治理与上传补采需求.md)

## 任务清单

### T1: 前端执行结果失败语义化（修「无法定因」）

- **描述**：`postExecResult` / `postVmConsoleResult` / `waitForExecResult` / `confirmAgentExec`
  所有失败分支填充 `error_type` 与 `stderr`；risk 拒绝、WS 未连接、发送失败、等待超时分类明确。
- **文件变更**：`frontend/customer/src/stores/chat.ts`
- **验收标准**：任一失败路径回传的 `exec-result` 均含非空 `error_type`
- **依赖**：无
- **预计耗时**：2h
- **状态**：待开始

### T2: 熔断产出终态 tool_result（修「卡片假死」）

- **描述**：`react_engine.py` 熔断拒绝分支 yield 终态 `tool_result`
  （`status=failed`、`error_type=circuit_open`）；前端展示用户可读提示。
- **文件变更**：`backend/agent-service/app/adapters/agents/htp/react_engine.py`
- **验收标准**：熔断后前端卡片为失败终态，不再永久 running
- **依赖**：无
- **预计耗时**：2h
- **状态**：待开始

### T3: terminal_bridge 本地日志落盘与事件治理

- **描述**：desktop 模式默认落盘到 Windows 桌面（支持 `Desktop`/`桌面` 双候选与回退）；
  新增 `bridge.startup` / `bridge.connected` / `bridge.disconnected` / `ws.send_failed` /
  `exec.timeout` / `exec.rejected` / `log.dropped` 事件；`exec.done` 补 `stderr_preview`；
  支持日志级别阈值；启动日志明示最终路径。
- **文件变更**：`terminal_bridge/main.go`、`terminal_bridge/README.md`
- **验收标准**：Windows 桌面双击运行即生成完整 JSONL；多工单可按 `case_id` 检索
- **依赖**：无
- **预计耗时**：4h
- **状态**：待开始

### T4: 后端日志上传接口

- **描述**：新增 `POST /api/bridge-logs/upload`（multipart，JSONL 解析、去重、工单归档、
  开关控制、统计返回）；`/api/bridge-logs` 支持 `fallback_case_id`。
- **文件变更**：`backend/conversation-service/app/routes/bridge_logs.py`、
  `backend/api-gateway/app/routes/bridge_logs.py`、配置与 Helm values
- **验收标准**：重复上传不重复落库；无 `case_id` 条目按绑定工单归档；开关关闭返回 404
- **依赖**：T3（日志格式）
- **预计耗时**：3h
- **状态**：待开始

### T5: Custom-UI 上传入口与失败提醒

- **描述**：新增「上传 terminal_bridge 日志」入口（选择/拖拽、进度、结果统计）；
  Agent 未排查出结果（步数耗尽 / 熔断 / 连续失败）时主动提醒上传；入口受开关控制。
- **文件变更**：`frontend/customer/src/components/*`、`frontend/customer/src/stores/chat.ts`
- **验收标准**：可上传并看到 accepted/duplicates 统计；关闭开关后入口隐藏
- **依赖**：T4
- **预计耗时**：3h
- **状态**：待开始

### T6: 前端异常上报与丢弃计数

- **描述**：新增 `window.onerror` / `unhandledrejection` 上报（service_name=customer-ui）；
  `forwardBridgeLog` 丢弃计数与告警，不再静默。
- **文件变更**：`frontend/customer/src/main.ts` 或 `App.vue`、`stores/chat.ts`、后端 client-logs 路径
- **验收标准**：前端未捕获异常可在库中检索到
- **依赖**：T4（复用上传/落库能力）
- **预计耗时**：2h
- **状态**：待开始

### T7: 入口访问日志与保留期治理

- **描述**：补齐入口访问日志（Traefik / customer-ui nginx），明确 Loki 保留期配置；
  在配置契约中登记上传开关。
- **文件变更**：`deploy/helm/hci-platform/*`、`deploy/config/config-contract.yaml`
- **验收标准**：`exec-result` 请求可在入口层日志检索；Loki 保留期显式配置
- **依赖**：无
- **预计耗时**：2h
- **状态**：待开始

## 任务依赖图

```
T1 ──┐
T2 ──┼── 前端/后端并行
T3 ──┴──► T4 ──► T5
            └──► T6
T7 （独立）
```

## 执行顺序建议

1. 第一批（并行）：T1、T2、T3、T7
2. 第二批：T4
3. 第三批（并行）：T5、T6
4. 收尾：文档更新 + 测试 + PR

## 文档更新计划

- [ ] `terminal_bridge/README.md` - 本地日志与事件字典
- [ ] `docs/deploy/部署指南.md` - 上传开关与入口访问日志
- [ ] `deploy/config/config-contract.yaml` - 新增开关登记
- [ ] `README.md` 第一屏 - 功能清单

## 测试计划

- 单元测试：路径解析、JSONL 解析去重、error_type 映射
- 集成测试：上传接口端到端
- 人工测试：Windows 桌面落盘 → 断连失败 → 上传 → 云端检索
