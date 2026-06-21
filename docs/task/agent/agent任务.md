---
status: active
category: task
audience: developer
last_updated: 2026-06-20
owner: team
update_trigger: Agent 层功能新增/重构/问题修复任务
---

# 任务：Agent 层

> 对应 方案文档：[../solution/agent/AI助手设计.md](../solution/agent/AI助手设计.md)（待建）

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-06-21 | v1.4 | Skill 调用失效修复（PR #475）：实施分层改进方案 — P0（preferred_next_steps 嵌入 sop_advance/get_sop_node 返回体）+ P1（软推荐门禁层 skill_call/tool_call）+ P2（S0/S1 系统提示词变量采集规范） | [skill调用失效根因分析与改进方案](../../solution/agent/skill调用失效根因分析与改进方案.md) |
| 2026-06-20 | v1.3 | Skill 调用失效根因分析（工单 Q2026062036731 实例）：确认 `hci-alert-parsing`/`hci-disk-vendor-lifetime` 未触发根因为变量门禁盲区，输出分层改进方案（preferred_next_steps 嵌入 + 软推荐门禁层 + 提示词规范） | [skill调用失效根因分析与改进方案](../../solution/agent/skill调用失效根因分析与改进方案.md) |
| 2026-06-08 | v1.2 | 排障 Agent 可靠性改造（PR #416）：阶段零~二完整落地，阶段三/四主体完成（T1-2 前端 exec_id 回传、T3-3 CoT 强制外显、T4-4 CI 回归门禁待后续 PR 整改），详见 [Agent 可靠性改造任务清单](./Agent可靠性改造任务清单.md) | [Agent 可靠性改造任务清单](./Agent可靠性改造任务清单.md) |
| 2026-05-31 | v1.1 | 助手类型命名统一（PR #369）：scheduler-service config.py 助手 display_name 改为 HTP/OPS/PAI Agent（移除 GLM-5 后缀），与 Helm configmap.yaml 同步 | — |
| 2026-04-05 | v1.0 | 初版 | [2026-04-02-S0意图识别与分类基线重构方案](../solution/events/2026-04-02-S0意图识别与分类基线重构方案.md) |

---

## 当前任务清单

| 状态 | 任务 | 创建日期 | 关联方案 |
|------|------|---------|---------|
| 进行中 | [Agent 可靠性改造（4 阶段）](./Agent可靠性改造任务清单.md) | 2026-06-08 | [Agent 可靠性三方案对比分析](../../solution/agent/Agent可靠性三方案对比分析.md) |
| ✅ 已完成 | Skill 调用失效修复（PR #475）：变量门禁盲区 + preferred_next_steps 引导 + 系统提示词规范 | 2026-06-20 | [skill调用失效根因分析与改进方案](../../solution/agent/skill调用失效根因分析与改进方案.md) |
