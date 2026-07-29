---
status: proposed
category: requirement
audience: product, developer, tester, operator
last_updated: 2026-07-28
owner: team
---

# Agent 并发测试与 KBD 驱动 HCI 仿真环境需求

## 1. 背景

为了验证 Agent 的真实排障能力，通常需要先在 HCI 环境中构造案例对应的真实故障，再通过 Custom UI 和 terminal_bridge 连接到 HCI 执行命令。实际过程中，大量 HCI 故障依赖复杂的集群状态、服务时序、磁盘/网络条件或内部组件行为，构造成本高、复现稳定性差，无法支持持续回归。

典型场景如下：

```text
Agent 选择 KBD 关键信号
  -> 生成/调用 qfk_log
  -> terminal_bridge 通过 SSH 执行
  -> HCI 返回日志
  -> Agent 获取 observation
  -> matcher 判定信号 PASS/FAIL/UNKNOWN/ERROR
```

例如：

```bash
acli log get -k "too many file" -p /sf/log/today/sfvt_vtpdaemon.log
```

该命令应返回与 KBD 关键信号期望一致的日志片段，Agent 才能继续完成证据判定和诊断报告。

## 2. 新增规模约束

本需求不仅要求模拟单个 HCI 环境，还要求支持 **100 个以上并发测试环境/测试场景**，以满足 Agent 并发能力、稳定性和效果回归测试。

这里的“100+ 环境”必须区分两种含义：

1. **100+ 逻辑隔离场景**：每个场景拥有独立的变量、fixture、SSH 会话和执行结果，但可以共享同一组模拟器进程。
2. **100+ 物理/容器隔离 HCI 实例**：每个测试实例有独立 SSH 服务和文件系统，适用于需要状态隔离或并行变更的场景。

首期应优先支持第一种，并为第二种预留调度接口。若把每个逻辑场景都部署成完整 HCI Pod，资源和启动时间会显著增加，不能作为默认方案。

## 3. 目标

### 3.1 功能目标

- Agent 可以按照正常生产路径通过 terminal_bridge 连接模拟 HCI。
- KBD 的 `signals_json` v2 可以生成测试场景草稿。
- KBD 的每条关键信号可以绑定 positive、negative、near-miss、timeout、permission-denied 等 fixture。
- 多条 KBD 的关键信号可以在同一模拟 HCI 环境中同时满足。
- 100+ 逻辑测试场景可以并发执行且彼此隔离。
- 模拟执行和真实执行使用同一套 `exec_id`、`trace_id`、`traceparent`、`artifact_id`、`evaluation_id` 链路字段。
- 未配置的命令必须返回明确错误，不能默认返回成功或空输出。
- KBD revision 变化后，旧 fixture 能被识别为 stale，不能静默继续使用。

### 3.2 测试目标

至少覆盖以下能力：

- KBD 候选全集加载和完整性；
- producer/consumer 变量依赖；
- qkv/qfk 工具选择；
- command 构造和规范化；
- 多节点、container、timeout、stdout/stderr；
- terminal_bridge WebSocket/SSH 执行链路；
- output filter 和输出预算；
- matcher 确定性判定；
- CDD 候选状态归约和 Conclusion Gate；
- Agent tool_call/tool_result 生命周期；
- 失败、超时、权限错误和未知 fixture 的 fail-closed 行为；
- 并发执行下的 trace、artifact 和结果不串线。

## 4. 非目标

首期不要求：

- 完整虚拟化所有 HCI 组件及真实故障注入；
- 从图片 OCR 自动生成可信的最终命令输出；
- 将 KBD 业务规则硬编码到 terminal_bridge；
- 在生产环境启用 replay 或模拟输出；
- 因没有 fixture 而阻止 KBD 在真实环境中诊断；
- 让 LLM 自由生成并直接发布 fixture。

## 5. 核心业务规则

1. Fixture 模拟的是工具 observation，不是 Agent 结论。
2. KBD matcher 是判定规范，不应被测试桩无条件“迎合”。
3. 图片、案例正文和 OCR 结果只能作为 fixture 生成辅助证据；最终 stdout/stderr 必须可审查、可版本化。
4. positive fixture 必须配套 negative 或 near-miss fixture，避免测试只验证“关键字存在”。
5. `ERROR`、`UNKNOWN`、`BLOCKED` 不得被转换为 `FAIL` 或 `PASS`。
6. 未知 command、未知 fixture、revision 漂移必须 fail closed。
7. 测试输出和真实输出的协议、trace 和 artifact 字段必须一致，只能额外增加模拟标识。

## 6. 易用性需求

KBD 导入和发布后，管理员应能：

1. 从 KBD 详情页生成测试场景草稿；
2. 查看自动解析出的 signal、tool、args、requires、produces、matcher；
3. 一键生成简单 matcher 的最小 positive/negative witness；
4. 上传或编辑 realistic stdout/stderr；
5. 配置 chunk、延迟、exit_code、timeout 和权限错误；
6. 运行单个信号、单个 KBD、分类内全部 KBD 或 100+ 场景批量回归；
7. 查看每次运行的 trace、exec、fixture、evaluation 和候选结论；
8. 在 KBD 修改后收到 stale fixture 提示；
9. 只将 validated/published fixture 纳入 CI 和批量测试。

## 7. 成功标准

首期验收目标：

- 100+ 逻辑测试场景可以并发排队和执行；
- 至少 20 个核心 KBD 有 validated fixture；
- 至少 1 个 KBD 包含 producer/consumer 多信号链；
- 正向 fixture 稳定得到预期 PASS；
- 负向和 near-miss 稳定得到预期 FAIL；
- timeout/permission/unknown fixture 稳定得到 ERROR/BLOCKED；
- 未知 fixture 不返回成功；
- 100 个并发测试的 trace、artifact、exec_id 不串线；
- 端到端结果可以从 `trace_id -> exec_id -> fixture_id -> evaluation_id -> conclusion` 完整追溯。
