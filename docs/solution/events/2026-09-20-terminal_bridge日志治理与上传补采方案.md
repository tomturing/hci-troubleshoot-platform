---
status: active
category: solution
audience: developer
last_updated: 2026-09-20
owner: 平台研发
---

# terminal_bridge 日志治理与上传补采方案

## 背景与需求

引用需求：[2026-09-20-terminal_bridge日志治理与上传补采需求](../requirement/events/2026-09-20-terminal_bridge日志治理与上传补采需求.md)

要解决的核心问题：**诊断失败后无法只凭云端日志定因**，以及**熔断后命令卡片假死**。

## 方案概述 (WHAT)

1. **本地最全日志**：terminal_bridge desktop 模式默认在 Windows 桌面落盘完整 JSONL 日志（附轮转、脱敏）。
2. **云端补采**：Custom-UI 提供手动上传入口，后端新增上传接口解析 JSONL、按工单归档去重落库。
3. **语义化失败**：执行结果回传强制携带 `error_type`，熔断拒绝产出终态 `tool_result`。
4. **前端可观测**：新增浏览器异常上报与丢弃计数，补齐入口访问日志与保留期。
5. **可屏蔽**：上传入口受配置开关控制。

## 详细设计

### 架构变更

```
┌──────────────┐  WS bridge_log   ┌──────────────┐  POST /api/bridge-logs   ┌──────────────┐
│terminal_bridge│ ───────────────► │ Custom-UI    │ ───────────────────────► │conversation  │
│  (本地 JSONL) │                  │ (自动回采)    │                          │  -service     │
└──────┬───────┘                  └──────┬───────┘                          └──────┬───────┘
       │ 桌面落盘（新增，默认开）           │ 手动上传（新增）                          │ 新增上传接口
       ▼                                 ▼                                        ▼
  bridge-YYYYMMDD-<inst>.log  ──►  POST /api/bridge-logs/upload  ──►  bridge_execution_logs
```

### 数据模型

复用 `bridge_execution_logs`，不新增表。上传链路补充的字段语义：

| 字段 | 上传场景语义 |
|------|-------------|
| `case_id` | 上传时绑定的工单；条目自带 `case_id` 时以条目为准，缺失则用绑定值 |
| `service_name` | `terminal_bridge`（bridge 日志） / `customer-ui`（前端异常） |
| `event_id` | 去重键，重复上传 `ON CONFLICT DO NOTHING` |
| `extra.upload_id` | 上传批次 ID，无 `case_id` 条目靠它追溯 |
| `extra.source` | `ws_realtime` / `manual_upload` |

### 接口设计

**新增 `POST /api/bridge-logs/upload`**（conversation-service，经 api-gateway 转发）

- 请求：`multipart/form-data`
  - `file`（可多文件）：JSONL 日志文件
  - `case_id`（可选）：绑定工单
  - `bridge_instance_id`（可选）
- 限制：单文件 ≤ 20 MiB、单次 ≤ 5 文件、单文件 ≤ 200000 行
- 响应：`{ ok, accepted, duplicates, skipped, invalid, upload_id, files: [...] }`
- 鉴权：复用 `_check_session_or_internal`
- 开关：`settings.bridge_log_upload_enabled`（默认 true；关闭返回 404 且前端隐藏入口）

**扩展 `POST /api/bridge-logs`**：`BridgeLogEntry` 允许 `case_id` 为空且请求体带 `fallback_case_id` 时由服务端补全，
避免无 `case_id` 条目被静默丢弃。

### 关键技术点

#### 1. 本地日志路径解析（Windows 桌面）

```
优先级：
1) HCI_BRIDGE_LOG_DIR           显式指定
2) desktop 模式 + HCI_BRIDGE_LOG_TO_DESKTOP != "false"：
   %USERPROFILE%/Desktop        （英文系统）
   %USERPROFILE%/桌面            （中文系统，目录存在则优先）
3) 回退 %LOCALAPPDATA%/HCI/TerminalBridge/Logs
4) 再回退 <可执行文件目录>/logs
```
最终路径在启动日志 `bridge.startup` 中显式输出，避免"不知道日志在哪"。

#### 2. 工单隔离（单文件 + 字段隔离）

- 落盘：**单一 JSONL 文件**（保证时序完整、一次上传即全量），文件名 `bridge-<YYYYMMDD>-<instance8>.log`。
- 隔离：每行携带 `case_id / conversation_id / exec_id / trace_id`；
  上传后云端按 `case_id` 归档检索。
- 关联：同一工单的 `exec.request → exec.start → exec.done` 通过 `exec_id` 串联，与 `trace_id` 打通后端链路。

#### 3. 事件字典扩展

| 事件 | 级别 | 触发点 |
|------|------|--------|
| `bridge.startup` | INFO | 进程启动，含版本/模式/监听地址/日志路径 |
| `bridge.connected` | INFO | WS 连接建立（含 custom_ui、origin） |
| `bridge.disconnected` | WARN | WS 连接断开（含断开原因） |
| `ws.send_failed` | ERROR | 出站消息发送失败（含 type/case/err） |
| `exec.request` / `exec.start` / `exec.done` | INFO/ERROR | 既有，补充 `stderr_preview` |
| `exec.timeout` | ERROR | 命令超时（显式事件） |
| `exec.rejected` | ERROR | 参数缺失/会话不存在/风险拒绝（含 `error_type`） |
| `log.dropped` | WARN | 日志丢弃（无 case_id、队列溢出）计数 |

#### 4. 失败语义枚举 `error_type`

`ws_not_connected` / `ws_send_failed` / `wait_timeout` / `user_rejected` / `risk_rejected` /
`circuit_open` / `session_missing` / `timeout` / `nonzero_exit` / `unknown`

前端在 `postExecResult` / `postVmConsoleResult` 的所有失败分支必须填充该字段与 `stderr`。

#### 5. 熔断终态

`react_engine` 熔断拒绝分支：除既有 ERROR 日志外，**必须** yield 终态 `tool_result`
（`status=failed`、`error_type=circuit_open`、`error` 文案用户可读），经 SSE 推给前端，
使卡片从 `running` 收敛为 `failed`。

## 决策依据 (WHY)

### 本地日志文件布局方案选择

| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| **A. 单文件 JSONL + 字段隔离（选中）** | 时序完整、一次上传即全量、实现简单、跨工单关联天然保留 | 需按 case 过滤检索 | ★★★★★ |
| B. 按工单分文件 | 隔离直观 | 文件数暴涨、跨工单时序丢失、上传需多选、清理复杂 | ★★★☆☆ |
| C. 单文件 + 每工单索引文件 | 兼顾 | 索引与正文一致性维护成本高 | ★★☆☆☆ |

**为什么选 A**：根因定位依赖**时序**（如"连接在命令下发前断开"），多文件会打散时序；
而隔离需求本质是检索需求，由字段 + 云端索引解决即可。

### 为什么不做「bridge 自发 HTTP 上报」

bridge 是通用代理，不感知后台地址（既有设计约束，`main.go:1985-1995`）；
且桌面环境出网策略不可控。本地落盘 + 浏览器/人工上传，保持 bridge 零后台依赖。

### 为什么上传接口走 multipart 而非复用 JSON 批量接口

JSONL 文件可能达数十 MB，JSON 批量接口会把整文件解析为对象数组，内存与请求体限制不友好；
multipart 流式读取 + 逐行解析，内存可控且便于进度统计。

### 权衡与妥协

- 单文件上限 64 MiB 是容量与可用性的折中；超限轮转保留 1 个历史文件（`bridge-*.log.1`）。
- 上传条目去重依赖 `event_id`；若日志被用户裁剪导致 `event_id` 缺失，则按
  `(case_id, bridge_instance_id, seq)` 兜底去重，仍无法匹配时按新条目落库（可能少量重复，可接受）。

## 影响范围

### 受影响的模块

- `terminal_bridge/main.go`：日志落盘默认化、路径解析、事件扩展、级别阈值
- `frontend/customer/src/stores/chat.ts`：失败语义、异常上报、丢弃计数、上传调用
- `frontend/customer/src/components/*`：上传入口 UI、熔断提示
- `backend/conversation-service/app/routes/bridge_logs.py`：新增上传接口、fallback_case_id
- `backend/agent-service/.../react_engine.py`：熔断终态
- `deploy/helm/hci-platform/*`：上传开关、入口访问日志、Loki 保留期

### 需要更新的文档

- [ ] `docs/solution/可观测性设计.md`（如存在）
- [ ] `docs/deploy/部署指南.md`
- [ ] `terminal_bridge/README.md`
- [ ] `README.md` 第一屏

### API 兼容性

`/api/bridge-logs` 保持兼容（新增可选字段 `fallback_case_id`）；
新增 `/api/bridge-logs/upload` 为纯新增。

## 实施计划

见 [任务文档](../../../../docs/task/events/2026-09-20-terminal_bridge日志治理与上传补采任务.md)。

## 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 桌面目录识别失败（中文系统） | 中 | 中 | 双候选 + LOCALAPPDATA 回退 + 启动日志明示路径 |
| 上传大文件超时 | 中 | 中 | 大小/行数限制 + 分片 + 前端进度与压缩提示 |
| 日志含敏感信息 | 高 | 低 | 沿用 `redactSensitiveText` 与 `sanitizeExtra` |
| 熔断终态改动影响既有流 | 中 | 低 | 仅在拒绝分支新增终态事件，不改正常路径 |
| 上传开关误关导致无法补采 | 中 | 低 | 默认开启，关闭需在配置契约登记 |

## 测试策略

- 单元：路径解析（Desktop/桌面/回退）、JSONL 解析与去重、error_type 枚举映射
- 集成：上传接口端到端（含重复上传、无 case_id 补全）
- 人工：Windows 桌面运行 bridge 生成日志 → 断开浏览器触发失败 → 上传 → 云端检索

## 验收标准

- [ ] 桌面自动落盘且路径可预期（启动日志明示）
- [ ] 多工单日志可按 `case_id` 检索且不串扰
- [ ] 失败记录均带明确 `error_type`
- [ ] 熔断后卡片为失败终态并可见提示
- [ ] 上传接口幂等（重复上传不重复落库）
- [ ] 开关可关闭入口
