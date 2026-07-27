---
status: active
category: verify
audience: developer
date: 2026-07-27
environment: dev
owner: team
---

# Terminal Bridge 端到端可观测性 P0 验收报告

## 1. 验收结论

dev 环境的单 Trace 主链、五类观测后端、Agent 调优证据字段和七类故障注入均按本文证据验收。
本结论只适用于当前 WSL K3s dev 运行态；ArgoCD 自动同步仍保持暂停，代码及环境仓改动通过 PR 进入远端并完成同步前，不得把本地运行态等同于可持续部署状态。

HCI 后台未部署 OTel SDK，因此 `terminal_bridge.ssh.exec` 是 HTP 对 HCI 命令执行的客户端 Span 边界；本文不宣称能够看到 HCI 内部进程 Span。

## 2. 环境

- WSL Ubuntu、K3s，业务 namespace `hci-dev`，观测 namespace `hci-observability`；
- Customer UI：`http://172.26.170.187.nip.io/`；
- Windows Edge 加载真实页面 JavaScript，通过同源 `/terminal-bridge` 连接 K3s Pod；
- Bridge 使用同一 Go 源码构建 Linux Pod 和 Windows EXE；
- HCI SSH：真实后台节点，只执行 `uname -a`、`false`、`sleep 5` 和大输出读取命令，不执行配置变更。

验收记录不保存私钥、密码、token、API key 或完整大输出。

## 3. 单 Trace 主链

真实主链样本：

| 字段 | 值 |
|---|---|
| case_id | `Q2026072744265` |
| conversation_id | `3372e334-1212-4c04-9064-4c26d612fb44` |
| exec_id | `08e1ab7b-13c5-4b16-b3d9-47e3d7a8047f` |
| trace_id | `206445916059e5aa3cc6dcc205754ac2` |
| artifact_id | `2f35e6be-a59f-5d5b-be74-249b203b0fc8` |
| 命令 | `uname -a`（只读） |
| 结果 | exit 0，stdout 94 字节，未截断 |

真实 Edge 捕获到：

```text
traceparent=00-206445916059e5aa3cc6dcc205754ac2-452789ff91c49407-03
HTTP 200 /api/conversations/.../exec-result
HTTP 200 /api/bridge-logs
```

Tempo 返回 317 个 Span，同一 Trace 中包含：

- `agent-service`、`api-gateway`、`case-service`、`conversation-service`、`kb-service`、`terminal-bridge`；
- `POST /internal/conversations/{conversation_id}/agent-exec`；
- `terminal_bridge.websocket.receive`；
- `terminal_bridge.ssh.exec`；
- `terminal_bridge.websocket.result.send`；
- API Gateway 与 Conversation Service 的 `POST /exec-result` Span。

### 3.1 修复前后的 Trace 差异

Python OTel 1.44 生成的 W3C flags 为 `03=Sampled(01)+Random(02)`。Go OTel v1.24 对 version 00 的保留位解析过严，静默丢弃父上下文，导致 Agent Trace 与 Bridge Trace 分裂。

兼容层只把 `02→00`、`03→01`，保留 Trace ID、Parent Span ID 和 Sampled 位；其他保留位继续由官方解析器拒绝。修复前存在 Agent/Bridge 双 Trace，修复后上述样本全程使用 `206445...`。

## 4. 五类观测证据

### 4.1 Tempo

同一 Trace ID 覆盖 Agent 发起、Conversation SSE、Bridge 三个真实 Span、结果回传 Gateway/Conversation，满足因果链要求。

### 4.2 Loki / Alloy

按主链 Trace 查询得到同一 exec_id 的：

```text
exec.request
exec.start
exec.done
```

Bridge JSON 包含 event/instance/seq、trace/span、exec、hash、字节数、截断和错误分类。高基数 ID 作为 structured metadata/JSON 字段，不作为 Prometheus label。

### 4.3 PostgreSQL Artifact / Tool Audit

Artifact：status=success、exit_code=0、duration_ms=9、stdout_bytes=94、stderr_bytes=0、stdout/stderr SHA-256 完整、均未截断。

Tool audit：status=committed、duration_ms=195、`trace_id == bridge_trace_id`、artifact_id 与输出 hash 可命中 Artifact。重复提交相同 exec_id 返回 HTTP 200 和“执行结果已幂等接收”，Artifact 行数保持 1。

### 4.4 Prometheus

原 Prometheus 运行态错误扫描 `hci`，而业务实际位于 `hci-dev`，导致业务指标为空。修正并滚动重启后发现 9 个 hci-dev targets；conversation-service 原有 scrape 注解但 `/metrics` 为 404，补齐端点并上线后 target 为 `up`。

已查询到：

- `bridge_build_info`、`bridge_exec_commands_total`、`bridge_exec_command_errors_total`；
- `agent_tool_call_total{tool_name="bash_exec",status="success"}`；
- `agent_tool_execution_duration_seconds_count{tool_name="bash_exec",status="success"}`；
- 失败故障期间 Bridge error counter 增长。

Prometheus 配置通过 `subPath` 挂载，调用 `/-/reload` 仍读取旧 inode；必须 rollout restart。该结论已归档为 D-018。

### 4.5 Langfuse

`tool.bash_exec` observation 类型为 TOOL，并与 OTel Trace ID 对齐。metadata 包含：

- `exec_id`、`session_id`、`risk_level`；
- `otel_trace_id`、`artifact_id`；
- `stdout_sha256`、`stderr_sha256`；
- 稳定错误分类 `error_type`，成功显式保存为 `none`，失败使用 `nonzero_exit`、`timeout` 等低基数值。

Langfuse 用于 LLM/Agent 语义与工具选择调优；Tempo、Loki、Prometheus 分别负责系统因果链、事件取证和聚合告警，不能互相替代。

最终调优样本为 exec `f199fc09-7fae-4589-abd7-50382bf7b36a`、Trace
`1c8f0a40bd879e4247e6347d6fc9870d`、Artifact `48dc2885-7360-566d-9526-e64d179c32b7`：

- Langfuse `tool.bash_exec`：`otel_trace_id` 与 Trace 相同，`error_type=none`；
- Artifact：success、exit 0、302ms、stdout 94 字节、未截断；
- Tool Audit：committed、552ms、`trace_id == bridge_trace_id`；
- Tempo：287 Span、六个服务，包含三个 Bridge Span 和 Gateway/Conversation 的 exec-result Span；
- 重复 exec-result 返回幂等成功，重复 Bridge batch 返回 `accepted=0, duplicates=3`。

Agent 失败调优样本使用 Trace `f7077bb3342d25b55c7fb6234e1dc5ca`。其中 exec
`ab47df68-edcf-4e98-8c3a-e9a6509fe9b4` 在 Langfuse、Artifact、Tool Audit 和 Bridge 日志中均记录
`error_type=nonzero_exit`，Artifact 状态 failed、exit 1、Artifact ID
`637e556d-e816-5d4a-8ed7-c734e0977040`；Prometheus
`agent_tool_error_total{tool_name="bash_exec",error_type="nonzero_exit"}=3`。这证明失败分类不是只存在于
Bridge 直连测试，而是已被真实 ReAct 工具生命周期消费并进入调优数据面。

该样本还暴露出 Agent 在硬件命令失败后连续尝试多个替代探测命令；这不是观测缺口，而是可由
exec/error_type/trace 序列量化的后续策略优化输入（可设置同类失败去重、节点能力探测缓存和尝试预算）。

## 5. 七类故障注入

| 场景 | 运行态结果 | 判定 |
|---|---|---|
| 非零退出 | exec `c46ce330...`，exit=1，`nonzero_exit`，Trace ID一致 | 通过 |
| 超时 | 修复前 2 秒阈值仍耗时 5008ms；改为独立 SSH TCP 连接+Session 后，exec `60a8aa2f...` 耗时 2002ms，exit=-1、timed_out=true、`timeout` | 通过，且发现并修复真实 P0 缺陷 |
| 5 MB 大输出 | exec `44f25826...`，stdout_bytes=5000000、truncated=true、完整流 SHA-256=`03a7bd51...`，进程未失控 | 通过 |
| Bridge 日志重复 | 每个样本首次 accepted=3；原 batch 重放 accepted=0、duplicates=3 | 通过 |
| exec-result 重复 | 主链 exec_id 重发返回 HTTP 200、“执行结果已幂等接收”，Artifact 仍 1 行 | 通过 |
| 错误 Origin | `Origin: https://evil.example` WebSocket Upgrade 返回 HTTP 403、`origin not allowed` | 通过 |
| known_hosts 变化 | 临时 key A 首次 accept-new，相同 key 复验通过；同地址 key B 返回包含 Want 的 `knownhosts.KeyError` | 通过 |

故障注入脚本只输出 exec/trace、退出码、耗时、字节数、hash、截断和错误分类；不输出凭据、命令正文或 stdout/stderr。

## 6. 回归测试

- Go：`go test ./...` 通过，覆盖 03 flags、Origin、指标端点、有界捕获、脱敏和主机指纹变化；
- Python OTel：9 passed；
- Conversation contract + Bridge logs：18 passed；
- Windows Node 对两个浏览器验收脚本执行 `--check` 通过；
- Frontend terminal API：`tsc` 编译通过，4 项无 worker smoke assertion 通过；真实 Windows Edge 进一步覆盖同源 URL、03 traceparent 中继和结果回传。Vitest 4.1.5 在 WSL `/mnt/d` bind mount 下两种 worker pool 均在 60 秒启动超时，属于测试运行器/挂载环境限制，不是断言失败。

## 7. 运行态与恢复事项

最终运行值：`HCI_BRIDGE_EXEC_TIMEOUT_SECONDS=120`。conversation-service、agent-service、terminal-bridge 已使用 dev-local 镜像并 Ready；Prometheus 的 conversation-service target 为 up。

以下 Application 的 automated selfHeal 暂停：

- `argocd-root`；
- `hci-platform-dev`；
- `hci-platform-obs-dev`。

恢复条件：代码与 `/mnt/d/aihci/hci-platform-env` 环境值经 PR 进入远端，ArgoCD 同步后确认镜像、`hci-dev` scrape namespace、Alloy 和 Bridge 配置不再依赖手工运行态，再按原策略恢复。双层自愈风险已归档为 D-019。

## 8. 临时夹具清理

验收使用两个临时只读 SOP：主链 `e2e-terminal-bridge-p0-20260727` 和 Agent 失败调优
`e2e-terminal-bridge-agent-failure-20260727`。完成证据后均先删除对应 `sop_execution`、将历史
conversation 的 `sop_document_id` 置空，再删除 SOP；最终按来源前缀复核 fixture document 为 0。
测试工单属于 dev 验收数据，可按环境数据保留策略统一清理。

## 9. 证据文件

- `.local/terminal-bridge-p0-single-trace-confirmed.txt/.png`；
- `.local/terminal-bridge-fault-nonzero.json`；
- `.local/terminal-bridge-fault-large-output.json`；
- `.local/terminal-bridge-fault-timeout-afterfix-final.json`；
- `.local/terminal-bridge-p0-final-agent-tuning.txt/.png`。

`.local` 证据不提交 Git；本报告保留可复核标识和摘要，避免敏感数据进入仓库。
