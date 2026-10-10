# Dispatcher / 状态机 / 幂等资源管理避坑

## PIT-006：状态机转换未加分布式锁导致重复处理

并发场景下，状态转换前必须使用 `SELECT FOR UPDATE` 或 Redis 分布式锁，防止同一任务被多个 worker 同时处理。

## PIT-007：幂等键未覆盖所有入口

外部回调、重试队列、手动触发三个入口都需要幂等检查，只在其中一个入口加锁不够。

## PIT-008：Dispatcher 重启后未恢复 in-flight 任务

进程崩溃后处于 `running` 状态的任务不会自动回到队列，需要在启动时执行 `recover_stuck_tasks()`，把超时的 running 任务重置为 `pending`。

## V-017：自动取值执行器调用漏传 conversation_id + 泛化 except 吞掉真实原因

工单 Q2026100812343 复盘：`sop_request_variable` 的 `tool_call` 策略调用
`tool_executor.execute(acquisition_tool, tool_args)` 未透传 `conversation_id`，命中执行器
`缺少 conversation_id` 早退分支（`_extract_value` 得 `None`），命令从未下发；却被泛化
`except Exception` 归一为 `sop_tool_variable_acquire_failed："工具执行或取值失败"`，误导排查。

**规则：**
1. 跨层执行器调用必须显式透传会话级上下文（`conversation_id`/`case_id`），不能依赖共享单例的实例属性缺省值。
2. 失败分支必须按 `error_type` 分类落库并保留真实原因（契约错误 / 不可达 / 超时 / 取值为空 / 工具能力缺失），禁止大而全、模糊归一的错误信息。
3. SOP 编排模式下采集失败应"失败即接管"转人工，不得把控制权交回模型即兴敲命令。
