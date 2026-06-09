# 诊断分析阶段环境数据丢失与 FactStore 缺陷修复方案

针对工单 `Q2026060935237` 中出现的诊断中断报错以及提示“环境数据为空，无法开始诊断推理”的现象，本方案旨在修复三个层面的缺陷：请求端环境数据丢失、Agent 端信息校验过于严苛、数据库中缺失 `fact` 和 `claim_evidence_link` 表。

## User Review Required

> [!IMPORTANT]
> 1. **环境上下文数据加载时机调整**：我们将修改 `conversation_service.py`，允许在 `S0` 至 `S4` 阶段都加载环境上下文，这会确保后续的诊断定位阶段都能获取到最新的环境状态。
> 2. **数据表结构变更**：将在本地/开发数据库应用 `fact` 和 `claim_evidence_link` 的 DDL 建表脚本。这两张表仅为 `agent-service` 内的 `FactStore` 事实库存储提供持久化支持。

## Proposed Changes

---

### [Component: conversation-service]

#### [MODIFY] [conversation_service.py](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/conversation-service/app/services/conversation_service.py)
- 将获取环境数据（`context_info`）的判定条件从原来的 `current_stage == "S0"` 调整为 `current_stage in ("S0", "S1", "S2", "S3", "S4")`，以确保所有诊断推理阶段均可正确加载环境信息。

---

### [Component: agent-service]

#### [MODIFY] [evidence_builder.py](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/services/evidence_builder.py)
- 优化 `check_information_quality` 校验逻辑。若本次请求的 `env_context` 为空，应结合 `FactStore` 检查是否已存有历史事实。仅在两者均为空时，才拦截并生成“环境数据为空”的澄清请求。

---

### [Component: Database Schema]

#### [MODIFY] [desired_schema.sql](file:///mnt/d/aihci/hci-troubleshoot-platform/database/desired_schema.sql)
- 在 `desired_schema.sql` 尾部追加 `fact` 表和 `claim_evidence_link` 表的 DDL 结构及相关索引定义。

---

## Verification Plan

### Automated Tests
- 运行全量单元测试，特别是 `test_reliability_phase2.py` 和 `test_reliability_phase4.py`，验证信息质量检查与 FactStore 功能没有回归报错：
  ```bash
  uv run pytest backend/agent-service/tests/unit/test_reliability_phase2.py -v
  uv run pytest backend/agent-service/tests/unit/test_reliability_phase4.py -v
  ```

### Manual Verification
- 使用 `atlas schema apply --env local --auto-approve` 将 Schema 应用到本地数据库。
- 确认 `postgres-0` 容器中已成功创建 `fact` 和 `claim_evidence_link` 表。
