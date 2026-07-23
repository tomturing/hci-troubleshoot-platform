# V2 信号抽取：qfk_system 字段错配导致丢信号与说明错填关键字

- 日期：2026-07-23
- 关联：KBD27123（V1→V2 升级后信号抽取回归）
- 影响服务：backend/kb-service 信号抽取链路 + 信号 v2 JSON Schema 契约

## 问题现象

KBD27123 从 V1 升级到 V2 后实测两个问题：

1. **丢失关键信号**：V1 识别 3 个关键信号（1×qkv_task + 2×qfk_system），V2 仅识别 2 个（1×qkv_task + 1×qfk_system），少了一条 `qfk_system`。
2. **说明错填关键字**：QFK 抽取把"说明"内容错误放入"关键字"字段。例如"镜像文件占用检查"被放进 `match.pattern` / `resource_keyword`（UI 的"关键字"字段），导致说明为空、关键字错显；极端情况下该信号被整条丢弃，关键字与说明双双消失。

## 根因

- `shared/schemas/acquirer_args.py` 中 `qfk_system` 是 8 个 `qfk_*` 工具里**唯一没有 `resource_keyword` 字段**的。而抽取 Prompt 第 71 行把 `qfk_system` 与其他 `qfk_*`（均含可选 `resource_keyword`）并列描述，LLM 据此给 `qfk_system` 生成了 `resource_keyword`。
- 主流程顺序是**先 `_validate_signal` 再 `_enrich_signal`（清洗在其中）**。LLM 生成的 `resource_keyword` 在清洗前就被 strict schema 的 `validate_acquire_args` 以"含未注册字段"**拒绝，整条信号丢弃** → 现象 1。
- 原 `_clean_signal_description` 只迁移 `resource_keyword`，**从不覆盖 `match.pattern`**（QFK 的"关键字"字段），因此残留的错填无法纠正 → 现象 2。且 `resource_keyword` 被拒时信号整体消失，关键字与说明一起丢失。

## 修复

1. `shared/schemas/acquirer_args.py`：给 `qfk_system` 增加可选 `resource_keyword`，与 Prompt 及其他 7 个 `qfk_*` 对齐，杜绝因"未注册字段"被拒而丢信号；并重新生成 `qfk_system.schema.json` 契约（保持一致，避免 admin 保存时 422）。
2. `kb-service/app/routes/extract_signals.py::_clean_signal_description`：扩展两类兜底
   - `resource_keyword` 实为说明 → 迁回 `description`；
   - `match.pattern`（QFK 的"关键字"字段）实为**说明性长句**（含动作动词且长度 ≥ 6）→ 迁回 `description`、清空 `pattern` 并标 `provenance.needs_review=True`（精确匹配串无法凭空反推，交人工补，避免臆造）。
   - 短匹配串（如"镜像占用"/"docker"/"overlay2"）即便含动词也视为真实匹配串，**绝不误清空**。
3. `_enrich_signal`：`needs_review` 计算改为 `or provenance.get("needs_review")`，保留清洗阶段打的标记。
4. 补单元测试覆盖 `match.pattern` 迁移分支（直接调用 + 经 `_enrich_signal` 入口）。

## 验证

- kb-service 单元测试：137 passed（含新增 `match.pattern` 迁移用例）。
- 信号 v2 JSON Schema 契约校验：`uv run python scripts/ci/check_signal_schemas.py` 通过（drift 检测一致）。
- ruff：`All checks passed!`

> 注：KBD27123 实体数据不在本地沙箱库，且完整抽取链路依赖线上 LLM 网关。建议在线上环境对 KBD27123 重新抽取确认：预期恢复 **1×qkv_task + 2×qfk_system**，且"镜像文件占用检查"落在**说明**字段、关键字为精确匹配串。

## 影响范围

- backend kb-service 信号抽取链路；
- 信号 v2 JSON Schema 契约（仅 `qfk_system` 新增可选 `resource_keyword` 字段，向后兼容）。
