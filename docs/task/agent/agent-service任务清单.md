---
status: active
category: task
audience: developer
last_updated: 2026-05-26
owner: team
update_trigger: agent-service 重构/修复/新功能迭代
---

# 任务清单：Agent Service

> 关联方案：[agent设计.md](../../solution/agent/agent设计.md)  
> 本文件为任务总览索引，详细实现规格见 `events/` 下各子文件。

---

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-05-26 | v1.0 | 初版：基于 agent设计.md v5.2 完整梳理 |

---

## 任务优先级说明

| 优先级 | 含义 | 执行策略 |
|--------|------|---------|
| **P0** | 阻断运行时，服务完全不可用 | 立即修复，不开新分支直接 hotfix |
| **P1** | 核心功能缺失或高危 bug | 本迭代内完成，独立 feature 分支 |
| **P2** | 重构优化，提升可维护性 | 排期完成 |
| **P3** | 新功能（SOP 执行引擎全链路） | 按里程碑分批实现 |

---

## P0 紧急修复（服务当前无法正常运行）

> 详细规格：[events/2026-05-26-P0紧急修复任务.md](./events/2026-05-26-P0紧急修复任务.md)

| 任务 ID | 问题 | 根因 | 涉及文件 | 状态 |
|---------|------|------|---------|------|
| **T-AGT-01** | P-NEW-1：direct 模式 `async for` 误用 | `for chunk in` 应为 `async for chunk in` | `diagnostic_agent.py` L217 | ⏳ 待做 |
| **T-AGT-02** | P-NEW-2：react 模式 `async for` 误用 | `for event in` 应为 `async for event in` | `diagnostic_agent.py` L204 | ⏳ 待做 |
| **T-AGT-03** | P-NEW-4：ConfirmService 确认回路断裂 | `/interactive-response` 路由到 ACP，未调用 `confirm_service.submit_confirm()` | `routes/agent.py`、`confirm_service.py`、`conversation-service/routes/conversations.py` | ⏳ 待做 |
| **T-AGT-04** | PA1：PaiAgentAdapter `scp_client=None` | `main.py` 初始化时未注入 `scp_client` | `main.py` | ⏳ 待做 |

---

## P1 重要修复（核心功能缺陷）

> 详细规格：[events/2026-05-26-P1重要修复任务.md](./events/2026-05-26-P1重要修复任务.md)

| 任务 ID | 问题 | 严重度 | 涉及文件 | 状态 |
|---------|------|--------|---------|------|
| **T-AGT-05** | P-NEW-3：ReactEngine 双重执行工具（写操作两次） | 🔴 HIGH | `react_engine.py` | ⏳ 待做 |
| **T-AGT-06** | P4：`_build_sop_prompt()` 无 token 上限保护 | 🟡 高 | `investigation_agent.py` | ⏳ 待做 |
| **T-AGT-07** | P6：SOP 命中未传 `sop_document_id`，hit_count 无法更新 | 🟡 中 | `investigation_agent.py`、`agent_port.py` | ⏳ 待做 |
| **T-AGT-08** | PA2：pai-agent 绕过三轨路由，SOP 功能不可用 | 🟡 高 | `pai_agent_adapter.py` | ⏳ 待做 |
| **T-AGT-09** | P2：SOP 路由仅按 `updated_at` 排序，无语义相关性 | 🟡 高 | `kb-service/routes/route.py` | ⏳ 待做 |

---

## P2 重构任务（架构债偿还）

> 详细规格：[events/2026-05-26-Agent重构任务.md](./events/2026-05-26-Agent重构任务.md)

| 任务 ID | 任务名称 | 说明 | 依赖 | 状态 |
|---------|---------|------|------|------|
| **T-AGT-10** | TriageAgent 重构 | `intent_agent.py` → `triage_agent.py`，继承 BaseAgent | P0 修复完成 | ⏳ 待做 |
| **T-AGT-11** | InvestigationAgent 重构 | `diagnostic_agent.py`（S1-S4）→ `investigation_agent.py` | T-AGT-10 | ⏳ 待做 |
| **T-AGT-12** | RemediationAgent 重构 | `diagnostic_agent.py`（S5）→ `remediation_agent.py` | T-AGT-11 | ⏳ 待做 |
| **T-AGT-13** | 代码层语义命名 | `DiagnosticStage.S0_INTENT` → `DiagnosticStage.TRIAGE` 等 | T-AGT-12 | ⏳ 待做 |
| **T-AGT-14** | PA3：pydantic-ai 工具调用可观测 | 拦截 `on_tool_start`/`on_tool_end` 回调，yield `AgentStageUpdate` | — | ⏳ 待做 |
| **T-AGT-15** | PA4：system 消息静默丢弃修复 | `_openai_messages_to_pydantic()` 合并 system 消息到后续 user 消息 | — | ⏳ 待做 |
| **T-AGT-16** | P3：SOP top_k=3 候选仅取 `[0]` 优化 | P9 修复后取最高相关候选，或多 SOP 融合 | T-AGT-09 | ⏳ 待做 |
| **T-AGT-17** | P7：SOP 审核失败前端无告警 | approve 接口补充 `warnings` 字段；前端 SOP 管理页展示状态徽章 | — | ⏳ 待做 |

---

## P3 新功能：SOP 执行引擎（多叉决策树 + 导航工具化）

> 关联方案：[agent设计.md §12.6–§12.7](../../solution/agent/agent设计.md)  
> 详细规格分两块：数据库 + 核心引擎 + 变量池

### 里程碑 M1：数据库层（前置，无代码依赖）

> 详细规格：[events/2026-05-26-SOP执行引擎-M1数据库.md](./events/2026-05-26-SOP执行引擎-M1数据库.md)

| 任务 ID | 任务名称 | 说明 | 状态 |
|---------|---------|------|------|
| **T-AGT-18** | 创建 `sop_execution` 表（dbmate 迁移） | 含 UNIQUE(conversation_id)、CHECK(status IN ...)、2 个索引 | ⏳ 待做 |
| **T-AGT-19** | 补全 `diagnostic_item` INSERT 路径（BUG-06 真正修复） | 表和 archive 路径已有，S2/S3/S4/S5 各阶段缺 INSERT | ⏳ 待做 |

### 里程碑 M2：导航工具化核心引擎

> 详细规格：[events/2026-05-26-SOP执行引擎-M2导航工具化.md](./events/2026-05-26-SOP执行引擎-M2导航工具化.md)

| 任务 ID | 任务名称 | 说明 | 依赖 | 状态 |
|---------|---------|------|------|------|
| **T-AGT-20** | 新增 `get_sop_node` 工具 | 返回节点文字 + 子节点列表 | T-AGT-18 | ⏳ 待做 |
| **T-AGT-21** | 新增 `advance_sop` 工具 | 移动到子节点，写 `execution_log`，更新 `current_node_id` | T-AGT-18 | ⏳ 待做 |
| **T-AGT-22** | ReactEngine 动态注入 SOP 工具 | SOP 命中时注册 `get_sop_node`/`advance_sop`，消除 `_process_sop_mode()` 双轨 | T-AGT-20、T-AGT-21 | ⏳ 待做 |
| **T-AGT-23** | 中断恢复流程 | `sop_execution.status=active` 检测，构建恢复版 system_prompt | T-AGT-22 | ⏳ 待做 |

### 里程碑 M3：变量池实现

> 详细规格：[events/2026-05-26-SOP执行引擎-M3变量池.md](./events/2026-05-26-SOP执行引擎-M3变量池.md)

| 任务 ID | 任务名称 | 说明 | 依赖 | 状态 |
|---------|---------|------|------|------|
| **T-AGT-24** | `extract_sop_variables()` + 双向校验 | approve 流程：扫描占位符 → 推断 strategy → Undeclared(Error) / Orphan(Warning) | T-AGT-18 | ⏳ 待做 |
| **T-AGT-25** | 变量 JIT 获取（`sop_request_variable` 工具） | 按节点懒加载，阻塞等待用户输入/确认 | T-AGT-24 | ⏳ 待做 |
| **T-AGT-26** | 三路合并（SOP 重新导入后变量池维护） | 保留人工编辑字段，新增变量 auto_generated，消失变量 deprecated | T-AGT-24 | ⏳ 待做 |
| **T-AGT-27** | `advance_sop` 支持 `variables_extracted` 上报 | 工具参数追加，服务端按 schema 校验后写 `context_variables` | T-AGT-25 | ⏳ 待做 |
| **T-AGT-28** | 管理端变量编辑 API | `GET /api/admin/sop/{id}` 追加 `variable_schema`；新增 `PATCH /api/admin/sop/{id}/variable-schema` | T-AGT-24 | ⏳ 待做 |

---

## 依赖关系图

```
P0 修复（T-AGT-01/02/03/04）
    └── P1 修复（T-AGT-05/06/07/08/09）
            └── P2 重构（T-AGT-10→11→12→13）
                        └── M2 导航工具化（T-AGT-20/21/22/23）

M1 数据库（T-AGT-18/19）  ←─── 无代码依赖，可并行启动
    └── M2 导航工具化（T-AGT-20/21/22）
            └── M3 变量池（T-AGT-24→25→26→27→28）
```

---

## 各服务涉及文件汇总

### agent-service

| 文件路径 | 涉及任务 |
|---------|---------|
| `app/main.py` | T-AGT-04 |
| `app/adapters/agents/htp/diagnostic_agent.py` | T-AGT-01、T-AGT-02 |
| `app/adapters/agents/htp/react_engine.py` | T-AGT-05、T-AGT-22 |
| `app/adapters/agents/htp/confirm_service.py` | T-AGT-03 |
| `app/adapters/agents/htp/investigation_agent.py` | T-AGT-06、T-AGT-07、T-AGT-11 |
| `app/adapters/agents/htp/triage_agent.py`（新建） | T-AGT-10 |
| `app/adapters/agents/htp/remediation_agent.py`（新建） | T-AGT-12 |
| `app/adapters/agents/htp/tool_registry.py` | T-AGT-20、T-AGT-21、T-AGT-22 |
| `app/adapters/agents/pai/pai_agent_adapter.py` | T-AGT-08、T-AGT-14、T-AGT-15 |
| `app/domain/agent_port.py` | T-AGT-07 |
| `app/routes/agent.py` | T-AGT-03 |

### conversation-service

| 文件路径 | 涉及任务 |
|---------|---------|
| `app/services/conversation_service.py` | T-AGT-03 |
| `app/routes/conversations.py` | T-AGT-03 |

### kb-service

| 文件路径 | 涉及任务 |
|---------|---------|
| `app/routes/route.py` | T-AGT-09 |
| `app/routes/admin.py` | T-AGT-17、T-AGT-24、T-AGT-28 |
| `app/routes/sop_ingest.py` | T-AGT-26 |
| `app/services/sop_parser.py`（扩展） | T-AGT-24 |

### database

| 文件路径 | 涉及任务 |
|---------|---------|
| `database/migrations/` | T-AGT-18 |
