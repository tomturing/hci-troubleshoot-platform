---
status: active
category: solution
audience: [developer, agent]
last_updated: 2026-10-10
owner: team
update_trigger: SOP 编排执行门禁、失败接管与反幻觉契约变更
---

# SOP 编排执行门禁约束设计与需求

> 起因工单：`Q2026100812343`「磁盘坏道」SOP 未完整执行（`smartctl.real` 未执行）复盘。
> 本文是 SOP 导航编排模式（`htp-agent` 命中 SOP）下"引擎编排执行 vs 模型手工兜底"的**强制性契约**。

## 1. 问题现象与根因链

### 1.1 现象
SOP 引擎从第一个变量起就未推进一步（`sop_execution.completed_steps=[]`、`execution_log` 仅 `node_entered`），
全程由 LLM 手工敲 `acli_exec / bash_exec / jq / lsblk` 拼凑证据，最后产出了带幻觉的定量结论
（如臆造 `8001563222016 字节`，并被幻觉标注降级为"待验证"仍直出）。

### 1.2 根因链（三层）
1. **P0 直接缺陷**：`tool_call` 策略自动取值时 `engine.py` 调用 `tool_executor.execute(acquisition_tool, tool_args)`
   **未透传 `conversation_id`**；SOP 模式共享的 `CompositeToolExecutor` 单例 `self._conversation_id=None`
   → 命中 `缺少 conversation_id，无法执行 acli 工具` 早退 → `_extract_value` 得 `None`
   → 被泛化 `except` 归一为 `sop_tool_variable_acquire_failed："工具执行或取值失败"`（误导，命令根本没下发）。
   - 佐证：`bridge_execution_logs` 中该工单只有 LLM 手工 `-k "坏道"` / `jq` / `lsblk` 的 `exec.request`，
     **没有自动取值的 `-k 磁盘` 下发记录**。
2. **P0 门禁漏洞**：变量来源守卫门 `_check_variable_source_gate` 仅把 `env_injection/user_input/user_confirm`
   列为 `hard_blocked`，`tool_call/skill_call`（`SOFT_PREFERRED_STRATEGIES`）是软提示、不阻断。
   因此自动取值失败后，模型被"放行"去手工替代 SOP 声明的采集来源。
3. **P0 终局反幻觉缺失**：在关键变量（`disk_dev` 为空、`check_meth` 未取得）未解析/未确认时，
   `sop_advance` 仍可推进到结论节点并生成定量结论。
4. **放大因素（非主因）**：`qkv_alert` 命中"工具类别 qkv 无对应执行器"；采集容器 `jq` 缺 ONIGURUMA 致 `test()` 报错；
   SOP#4 `tree_json.variables[].alert_logs.source` 与已订正的 `variable_schema` 不一致。

## 2. 设计目标与原则

- **编排唯一性**：SOP 模式下，节点声明变量的采集只能由引擎经受控通道完成（`sop_request_variable` + 引擎执行声明采集步骤）。
  模型**不得**用裸 `bash_exec/acli_exec/qkv_*` 即兴替代声明来源。
- **失败即接管**：合法采集一旦失败，引擎立即接管终态，**绝不把控制权交回模型凑值/凑命令**。
- **一律转人工弹框**（本工单确认决策）：采集客观无法完成时，转 `user_input` 弹框让人工补值/确认；
  工单保持交互，不自动中止；人工未确认前维持 pending，不伪造、不推进结论。
- **可复盘**：所有执行失败必须回传准确、完整、细粒度的结构化错误与日志；禁止大而全、模糊归一的日志。

## 3. 门禁契约

### 3.1 守卫门（覆盖全部声明变量）
判定"节点窗口内已声明但未解析"的变量集合，纳入硬阻断的来源策略从
`{env_injection, user_input, user_confirm}` 扩展为再包含 `{tool_call, skill_call}`（即变量在 `variable_schema`
声明了 `acquisition_tool`，引擎有受控执行通道）。命中未解析声明变量时，任何非 `sop_*` 导航的裸工具调用
返回 `sop_variable_gate_blocked`，并携带 `next_tool_call=sop_request_variable`。

不变式：
- `SOP_NAVIGATION_TOOLS`（`get_sop_node/sop_advance/sop_request_variable`）永不阻断。
- 与当前节点窗口无关、且非声明来源的探索性工具调用不在本门禁范围（保持可用性）。

### 3.2 失败即接管状态机（一律转人工）
`tool_call/skill_call/derived/json_extract` 采集在透传 `conversation_id` 修复后仍失败的，不得 `return dict` 交回模型：

```
sop_request_variable(variable)
  ├─ 缓存命中 ─────────────────────────────▶ ok(value)
  ├─ 引擎执行采集成功 ─────────────────────▶ persist + ok(value)
  └─ 采集失败(分类 error_type) ────────────▶ _request_user_input(
          kind=variable_input,
          msg=<精确失败原因 + 已获证据>,
          error_type=<contract|unreachable|timeout|tool_capability_missing|empty_value|...>)
        → 阻塞等待人工补值/确认
        → 人工超时(Redis 120s)：维持 pending，不伪造、不推进
```

### 3.3 终局反幻觉门
当当前节点窗口存在**未解析或未人工确认**的声明变量时：
- `sop_advance` 推进到 solution/叶结论节点 → 硬阻断，返回结构化 `sop_conclusion_blocked_unresolved_variables`。
- 结论生成禁止"定量数值/百分比/字节数"等无证据来源断言；数字断言必须绑定工具输出证据，缺证据即拦截。
  - 落地（升级自"仅 re-run + 追加待验证标注"）：`react_engine` 在**流式输出前**对（re-run 后的）内容复检，
    `ungrounded_conclusion_block()` 判定「结论语境命中 `CONCLUSION_MARKERS` 且存在无法溯源数值」即硬拦截，
    将整段定量结论替换为受控文本 `UNGROUNDED_CONCLUSION_BLOCK_TEXT`（不含任何数字），记 `hallucination_hard_blocked_ungrounded_numbers`
    错误事件与 `ungrounded_number_hard_blocked` 指标，杜绝幻觉数值先抵达用户。
- 引擎改输出受控状态："该步受阻：原因=<证据>；已转人工确认"。

## 4. 精确错误分类（禁止模糊归一）
`error_type` 至少区分：
| 分类 | 触发 | 归属通道 |
|---|---|---|
| `contract_error` | 缺 `conversation_id` / 参数契约错误（如 TypeError） | engine |
| `node_unreachable` / `timeout` | 目标节点连接失败 / 命令超时 | terminal_bridge |
| `tool_capability_missing` | `jq` 缺 ONIGURUMA、`acli` 不在 PATH 等 | terminal_bridge |
| `device_absent` | 盘符为空 / 内核无此设备（本例：已摘除盘） | engine `_classify_*` + bridge `classifyExecFailure` + lsblk 探测 |
| `empty_value` | 工具执行成功但取值为空（如 `-k 磁盘` 无命中） | engine |
| `executor_unavailable` | 类别无对应执行器（qkv） | engine |

## 5. terminal_bridge 可观测性与探测（req2/req3）
当前实现已具备较完备的 `error_type` 分类、`blogContext` 结构化日志与 `exec_result` 全字段回传
（见 `docs/solution/observability/2026-07-27-terminal-bridge端到端可观测性重构设计.md`）。本次补强：

- **审计（req2）**：核对每个失败分支都产出 `exit_code / stderr_len / stdout_len / *_sha256 / *_truncated /
  duration_ms / timeout / error_type / exec_id / trace_id`；补齐 marker 模式 `execCommand` 超时分支的 `error_type`；
  `jq` ONIGURUMA 归为 `tool_capability_missing`；`UploadStatus=upload_failed/artifact_upload_disabled` 必须带原因与
  `artifact_id` 回传记录，不得静默。`hci-staging` 应用日志未入 Loki，故以 `bridge_execution_logs` 为权威事实源。
- **辅助探测（req3）**：命令超时 / SSH 连接失败 / 目标无响应时，触发一组廉价探测并各记 `event=probe.*`：
  1. `node_ip:22` TCP 可达；2. SSH banner/kex；3. bridge `/health/live`；4. 工具能力（`acli --version`、`jq --version` 含 oniguruma）；
  5. 现场最小探测（`lsblk` 整盘清单与命令引用的 `/dev` 盘符交叉比对，全部未命中即产出 `probe.conclusion=device_absent`，佐证盘已摘除）。探测结果用于区分网络不可达 / 认证失败 / 设备不存在 / 工具缺失 / 命令超时。

## 6. 环境与存量数据（req4 P2）
- **采集镜像 `jq`**：本仓库可控的采集/模拟镜像为 `hci_sim`（Alpine，`acli` 由 `hci-sim` 符号链接、并承载 bridge 下发命令的 SSH 服务端）。已在其运行层 `apk add --no-cache jq`（Alpine `jq` 默认链接 OnigURUMA），使 `jq ... test()/match()` 在本镜像内可用，规避 `tool_capability_missing`。
  **真实客户节点的 `jq` 由客户环境提供、不受本仓库构建控制**；对其唯一可行治理为「探测判别（`probe.tool_capability.jq.oniguruma`）+ `classifyExecFailure` 归 `tool_capability_missing` + SOP 采集模板下推 `-k` 粗筛规避 `test()/match()`」。
- 链路根变量 `alert_logs/node_ip/asan_disks/disk_dev` 声明 `fallback_strategy="user_input"`。
- 重编译 SOP#4 `tree_json`，使 `alert_logs.source` 与 `variable_schema` 对齐；新增 `database/data-migrations/` 迁移（带唯一调用链）。

## 7. 验收标准
1. 复现工单场景：自动取值真实下发命令；若节点声明变量未解析，裸 `acli_exec/bash_exec/qkv_*` 被门禁拦截。
2. 采集失败一律落到人工弹框，`message` 表不出现"模型手工拼命令凑数"的 `bash_exec/acli_exec` 兜底轨迹。
3. 关键变量未确认时，`sop_advance` 到结论节点被阻断，结论中无无证据数字断言。
4. `bridge_execution_logs` 对失败/超时/上传失败/探测均有可判别 `error_type` 与完整字段。
5. 单测 + Go 测试 + Q2026100812343 回归用例通过。

## 8. 落点索引
- `backend/agent-service/app/memory/variable_pool/engine.py`（A/B/§3.2/§4，含 `device_absent` 分类与 `_is_device_absent`）
- `backend/agent-service/app/adapters/agents/htp/react_engine.py`（§3.3 反幻觉硬拦截：`ungrounded_conclusion_block`/`CONCLUSION_MARKERS`/`UNGROUNDED_CONCLUSION_BLOCK_TEXT`）
- `backend/agent-service/app/adapters/agents/htp/sop_tools.py`、`app/tools/sop/nav.py`（B/§3.1/§3.3）
- `terminal_bridge/main.go`（D/E，含 `classifyExecFailure` 的 `device_absent` 与 `deviceAbsentFromProbe` lsblk 交叉判别）
- `hci_sim/Dockerfile`（§6 采集/模拟镜像带 OnigURUMA 的 jq）
- `database/data-migrations/`（F）
- 避坑：`docs/verify/pitfalls/dispatcher.md`（V-017）；观测：`docs/solution/observability/2026-08-17-诊断链路日志契约与门禁加固.md`
