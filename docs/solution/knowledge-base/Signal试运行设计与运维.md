---
status: active
category: solution
audience: developer, reviewer, sre
last_updated: 2026-08-27
owner: platform
---

# Signal 试运行设计与运维

## Admin UI 结果展示约定

试运行结果在主视图只展示一个业务终态：`PASS`、`FAIL` 或 `UNKNOWN`。其中 `PASS` 对应当前 Signal 完整后处理链判定通过，`FAIL` 对应判定不通过，`UNKNOWN` 表示输入、处理链或 AI 响应不足以可靠给出结论。终态由平台后处理链的判定结果决定，**不得根据 AI `output` 的类型或取值（包括 `1/0`、布尔值、字符串、数组和对象）推导或改写**。`AI status=success/insufficient` 仅属于原始响应字段，不与主视图终态并列展示；原始四字段 JSON（`status`、`output`、`evidence`、`reason`）通过“AI 原始响应详情”展开查看。

## 1. 边界

试运行验证的是**当前未保存 Signal 草稿对一组既有输入的执行结果处理**，不是执行 acquisition、aCLI 命令、SSH 或任何现场动作。

| 范围 | 输入 | 正式链路复用 | 禁止行为 |
|---|---|---|---|
| QFK | 完整 stdout/stderr 文本 | Extract -> AI（如配置）-> Matcher / produces | 调用 Handler、Terminal Bridge、写变量池 |
| QKV | 已投影 JSON records | 当前处理单元及其前序 derive/assert | 重新传入 QKV 原始响应、越过投影边界 |

`整个 Signal` 是该 Signal 的完整后处理链。`当前 AI 处理` 不是“半个 Signal”：QFK 仅允许其 `match.extract` 的 AI 步骤，QKV 仅允许点击的 AI derive 单元及其所需前序 derive，跳过不产出依赖的断言和最终 Matcher。任何处理结果均不是现场证据，除非经由下述 Bundle 验证资产流程保存。

## 2. 调用链与事实域

```text
Admin 草稿 + 独立数据集
  -> Gateway /api/v1/signals/dry-run
  -> Agent /internal/signal-dry-run
  -> Shared Extract / Matcher / QKV processing
  -> PreviewResult(trace_id, config_revision, input_sha256)

保存时：Gateway 重新执行同一 dry-run
  -> hci-sim verification-assets
  -> 追加受校验资产 + ReviseDraft
  -> 新的不可变 Bundle digest
```

浏览器从不获得内部 Token，也不能直接提交 PASS、资产摘要或 manifest。保存阶段由 Gateway 重新执行 dry-run，因此客户端篡改预览结果不能进入 Bundle。

## 3. 身份、隔离与失效

一次 Preview 的最小身份为：`support_id`、`kbd_revision`、`signal_id`、QKV 的 `processing_index`、`dataset_id`、`config_revision` 和 `trace_id`。

前端结果面板遵循互斥状态：请求尚未完成时显示等待/未返回说明；服务返回 `PASS`、`FAIL` 或 `UNKNOWN` 后仅显示该结果、处理值（支持结构化表单与 JSON 切换展示）和证据，不再显示“服务未返回结果”或空状态感叹号。

`config_revision` 是草稿 Signal 的稳定 SHA-256。后端每次自行复算；不匹配返回 `DRAFT_REVISION_MISMATCH`。输入、数据集切换或草稿编辑均会清空前端结果，禁止混用 A 数据集的 evidence 与 B 数据集的结果。

fixture/replay 只能从已发布 Bundle 的 `verification_assets` 读取 PASS 资产，Gateway 会按 `source_ref` 回读并覆盖浏览器 payload。临时样本不会写入 Bundle。保存时仍必须重新执行并通过草稿漂移校验。

## 4. Bundle 验证资产

`fixture.Manifest.verification_assets` 是 Bundle 对象的一部分，参与 Bundle digest。资产包含 KBD/Signal 绑定、来源、payload 摘要、配置修订和调用链。

- 仅 `PASS` 可保存。
- 若当前工单没有 Draft，Admin UI 会先通过 Gateway 按 C1 权威 KBD 快照创建唯一 Draft，再追加验证资产；多个 Draft 或 C1 capability gap 仍然 fail-closed。
- 追加资产权威继承目标 Bundle Manifest 的 `KBD.SupportID` 与 `KBD.Revision`（运行时修订号，如 `r1`），并严格校验请求体 `support_id` 防止跨 KBD 注入；前端组件绑定与控制面均对齐该不可变运行时快照版本，杜绝透传业务表自增 ID 导致不可变 Lint 失败。
- QFK 必须绑定到相同 Signal 的精确 Route，payload 是非空文本，并写入该 Route 的 `stdout`。
- QKV 保存已投影 records；不允许声明 Route，防止把 QKV 原始响应伪装成 stdout。
- payload 只存在于受控 Bundle 对象；数据库、指标和日志仅保存摘要、长度、状态及 trace。
- 追加资产永远调用 `ReviseDraft`，不能覆盖 draft/validated/published 父对象。

发布前，Bundle 的 schema/digest/RouteKey 校验仍由既有控制面执行。验证资产目前用于可复现实例输入；多样本 canonical variant 和“所有必需 Signal 覆盖”仍需在发布门禁中进一步落地，不能把单个样本等同于完整场景证明。

## 5. AI Prompt 依赖

AI 试运行与正式 QFK/QKV 处理共用数据库中的 `ai_processing` Prompt 槽位。Agent 启动时将数据库会话工厂注入试运行路由；Prompt 缺失或数据库不可用时返回结构化 `QFK_AI_PROCESSING_PROMPT_UNAVAILABLE`，不会使用代码内置 Prompt 伪造结果。部署后应确认 `system_prompt.ai_processing_v1` 和 `prompt_slot.ai_processing` 均为 active。

AI 处理成功后的试运行结果使用 `AIExtractionResult.reason` 作为可读证据说明，并保留 `evidence_line_numbers` 与 `evidence_lines` 作为原始定位信息；不得访问不存在的 `evidence` 字段。验证时应覆盖一次带 AI 后处理的真实 Gateway 请求，确认模型成功响应能返回 `PASS` 或明确的业务失败，而不是 500。

## 6. 可观测性与排障

Agent 暴露：

- `hci_signal_dry_run_total{scope,verification_scope,status,error_code}`
- `hci_signal_dry_run_duration_seconds{scope,status}`

日志事件 `signal_dry_run_completed`、`signal_dry_run_rejected`、`signal_dry_run_failed` 和 `bundle_factory verification_asset` 使用同一 `trace_id`。原始 payload 不写日志；排障使用 `input_sha256`、Signal、KBD revision 和 trace 关联 Tempo/Loki。

常见错误：

| 错误码 | 原因 | 处理 |
|---|---|---|
| `DRAFT_REVISION_MISMATCH` | 预览期间草稿已变化 | 使用当前草稿重新试运行 |
| `QFK_OUTPUT_EMPTY` / `QFK_NO_MATCH` | 输入不满足取值契约 | 检查数据来源与 Extract 配置 |
| `QFK_AI_EXTRACT_UNAVAILABLE` | Agent 未初始化 AI 客户端 | 检查 Agent 启动与 LLM 配置 |
| `verification_asset_route_not_found` | QFK Signal 与 Bundle Route 不一致 | 先重新生成/整理 Bundle Draft |

## 7. 验收

Python 单测覆盖 QFK PASS、草稿漂移拒绝、QKV 前序依赖执行及 AI 范围约束；Vue 测试覆盖入口与对话框。hci-sim 需要执行 fixture/controlplane Go 单测，验证资产追加必须生成新 digest、错误 Route/payload 被拒绝、父 Draft 不变。
