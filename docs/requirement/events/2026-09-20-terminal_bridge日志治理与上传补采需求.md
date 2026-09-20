---
status: active
category: requirement
audience: all
last_updated: 2026-09-20
owner: 平台研发
---

# terminal_bridge 日志治理与上传补采需求

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-09-20 | v1.0 | 初版 | 本文件 |

## 背景与问题

工单 **Q2026092010235** 中，AI 诊断执行 `smartctl.real --scan` 等命令时，前端逐条显示
「执行流已结束，但未收到对应的执行结果；请重新发起诊断」，最终触发「诊断次数已达上限」。

事后排查暴露两类缺陷：

1. **无法定因**：现有日志只能定位到「前端回传 `exit_code=-1` 且输出为空」这一层，
   无法区分「WebSocket 未连接 / 发送失败 / 等待超时 / 用户拒绝 / 熔断拒绝」；
   `bridge_execution_logs` 在故障窗口（09:35–09:40）**0 条记录**，最关键的 bridge 侧证据缺失。
2. **熔断后卡片假死**：`bash_exec` 熔断打开后，后续命令被本地拒绝、从未下发，
   也不产出终态 `tool_result`，前端卡片永久停在 `running`，SSE 结束时被兜底标记为失败。

### 核心痛点

| 痛点 | 证据 |
|------|------|
| 回传失败无语义 | `exec-result` 回传 `exit_code=-1`、`stderr` 为空，无法判别失败类型 |
| bridge 日志断供 | 本工单 bridge 日志最后一条 09:11:18，故障窗口 0 条；上报链路强依赖浏览器在线 |
| 无 case_id 日志被静默丢弃 | `chat.ts:180-182 forwardBridgeLog` 对无 `case_id` 条目直接 `return`，无计数 |
| 浏览器侧零可观测 | 无 `window.onerror` / `unhandledrejection` 上报，前端异常全部丢失 |
| 熔断无终态 | 被拒绝的命令不产出 `tool_result`，卡片永久 running |
| 入口层访问日志缺失 | `exec-result` 走 nginx 直连 conversation-service，绕过 api-gateway，网关侧无记录 |

## 需求描述

- **功能概述**：建立「本地最全日志 + 云端补采上传 + 全链路语义化失败原因」的日志治理体系，
  使任何诊断失败都能在 **不依赖用户复现** 的前提下定位到根因。
- **用户场景**：
  1. 运维在 Windows 桌面双击运行 `terminal_bridge.exe`，程序自动在**桌面**生成完整日志文件。
  2. 一个 bridge 同时服务多个工单时，日志按 `case_id` 逻辑隔离、可回溯到具体工单与执行。
  3. Agent 未排查出结果时，Custom-UI 主动提示用户「上传本地日志」，
     用户一键选择桌面日志文件上传，云端补齐缺失的 bridge 日志。
  4. 后续自动回采稳定后，可通过配置开关屏蔽手动上传入口。

## 功能需求

1. **本地日志落盘（必须）**
   - desktop 模式默认落盘，目录默认 **Windows 桌面**（`HCI-TerminalBridge-Logs`），支持环境变量覆盖与关闭。
   - 单文件 JSONL 全量（含启动、连接、收发、执行、错误全事件），带轮转与容量上限。
   - 中文/非英文 Windows 桌面目录需正确识别（`Desktop` 与 `桌面` 兼容），识别失败回退到 `%LOCALAPPDATA%`。
2. **工单隔离与关联（必须）**
   - 每条日志携带 `case_id / conversation_id / exec_id / tool_call_id / trace_id / node_ip / bridge_instance_id / seq / event_id`。
   - 上传时绑定工单，云端按 `case_id` 归档；无 `case_id` 的条目归入上传批次 `upload_id`，不丢弃。
3. **日志规范与丰富（必须）**
   - 新增事件：`bridge.connected` / `bridge.disconnected` / `exec.timeout` / `exec.rejected` / `ws.send_failed`。
   - `exec.done` 强制携带 `error_type`、`stderr_preview`；超时显式化为独立事件 `exec.timeout`。
   - 支持日志级别阈值（DEBUG/INFO/WARN/ERROR），保留既有脱敏与命令截断策略。
4. **失败语义化（必须）**
   - 前端回传执行结果时，`exit_code=-1` 必须携带 `error_type` 与 `stderr`，
     取值枚举：`ws_not_connected` / `ws_send_failed` / `wait_timeout` / `user_rejected` / `risk_rejected` / `unknown`。
5. **熔断终态（必须）**
   - 熔断拒绝执行时必须产出终态 `tool_result`（`status=failed`、`error_type=circuit_open`），
     并在前端可见提示，杜绝卡片永久 running。
6. **云端上传入口（必须）**
   - Custom-UI 新增「上传 terminal_bridge 日志」入口（选择/拖拽本地 JSONL 文件）。
   - 后端新增上传接口：解析 JSONL、去重落库、按工单归档、返回统计。
   - Agent 未排查出结果（步数耗尽 / 熔断 / 连续失败）时主动提醒上传。
7. **前端异常上报（必须）**
   - 新增 `window.onerror` / `unhandledrejection` 上报通道，落库可查。
   - `forwardBridgeLog` 丢弃条目需计数并告警，不再静默。
8. **开关治理（必须）**
   - 手动上传入口支持配置开关（Helm values + 前端可见性），自动回采稳定后可一键屏蔽。
9. **入口层访问日志（建议）**
   - 补齐入口访问日志记录，明确 Loki 保留期，保证事后可查。

## 非功能需求

- **性能**：本地日志写入带缓冲，不影响命令执行主链路；轮转上限默认 64 MiB。
- **可用性**：日志落盘失败不得影响 bridge 主功能（best-effort）。
- **安全**：延续既有脱敏（password/secret/token/private_key 等），上传文件大小与行数受限。
- **可维护性**：事件清单文档化，新增事件必须同步更新事件字典。

## 验收标准

- [ ] Windows 桌面双击运行 bridge，桌面自动生成日志文件，完整记录一次诊断全过程（含失败）。
- [ ] 同一 bridge 服务 2 个以上工单时，日志可按 `case_id` 完整筛选，且不串扰。
- [ ] 人为断开浏览器 / 断网后执行失败：云端能查到 `error_type` 明确的失败记录，无需复现即可定因。
- [ ] 熔断触发后，前端命令卡片显示失败终态与「工具熔断，暂不可用」提示，不再永久 running。
- [ ] Custom-UI 可上传本地日志，云端按工单补齐缺失日志，重复上传不产生重复数据。
- [ ] 手动上传入口可通过配置关闭，关闭后前端不展示入口。
- [ ] 前端未捕获异常可被上报并在库中检索到。

## 约束条件

- 技术约束：terminal_bridge 为 Go 单文件程序，Windows 桌面模式；前端 Vue3 + Pinia；后端 FastAPI。
- 依赖约束：复用现有 `bridge_execution_logs` 表与去重键（`event_id`），尽量不引入新表。
- 兼容约束：`/api/bridge-logs` 现有协议不得破坏。

## 风险与假设

- 风险：桌面日志文件可能较大，上传受网络限制 → 缓解：大小/行数限制 + 分片上传 + 提示压缩。
- 风险：用户桌面目录权限受限 → 缓解：回退 `%LOCALAPPDATA%` 并在启动日志中明示最终路径。
- 假设：自动回采链路（bridge → WS → 浏览器 → 后端）仍为主路径，手动上传为兜底补采。
