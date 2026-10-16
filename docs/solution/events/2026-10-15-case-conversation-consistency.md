---
status: active
category: solution
audience: developer
last_updated: 2026-10-15
owner: hci-dev
---

# 工单与对话一致性修复方案

## 背景与需求
引用需求文档：工单异常根因为 case_id 随机生成、工单↔会话非原子、前端缺降级。目标：对齐 case_id 契约、确保会话创建幂等、前端优雅降级。

## 方案概述 (WHAT)
R1 统一 case_id 生成到 DB 自增；R2 conversation 创建幂等 ensure；R3 前端 404 降级。

## 详细设计

### R1：case_id 改用 DB 自增
- 删除 `case_service._generate_case_id()`（random 版）。
- `create_case` 改为 `case_id = await self.repository.generate_case_id()`；`case_repo.generate_case_id()` 已存在，内部执行 `SELECT generate_case_id()`。
- DB 函数已验证：取当天 `TO_CHAR(CURRENT_DATE,'YYYYMMDD')`，事务级 advisory lock（按天串行），`MAX(SUBSTRING(case_id FROM 10 FOR 5))+1`，5 位补零返回 `Q{today}{seq}`。

### R2：conversation 幂等 ensure
- `conversation-service` `POST /api/conversations/`（create_conversation）改为幂等：若 `case_id` 已存在 conversation，返回已有；否则创建。语义等同 ensure。
- 前端 `createConversation()` 仍调用 create，但此时为幂等；`completeCaseCreationFlow` / `connectSSHAndCreateCase` 在创建工单后调用 `createConversation()`，失败重试 1 次。

### R3：前端 404 降级
- `loadConversationHistory(caseId)` 的 GET 若命中 404「工单不存在」，catch 后 `addSystemMessage('工单不存在，请重新创建工单')` 并 `currentCase.value = null`，避免裸 404 / 未捕获异常。

## 决策依据 (WHY)

| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| R1-A DB 自增（选中） | 对齐架构契约、序号连续可读、无碰撞 | 切换瞬间序号跳变（可接受） | ★★★★★ |
| R1-B random + 唯一约束重试 | 改动最小 | 放弃「当天自增」契约、序号无意义 | ★★☆☆☆ |
| R2 ensure 幂等（选中） | 天然防重复、调用方无需先查 | 需调整 create 接口语义 | ★★★★☆ |
| R2 跨服务分布式事务 | 强一致 | 复杂度高、易引入新故障 | ★★☆☆☆ |

### 为什么选 R1-A
schema 注释与 DB 函数早已声明「当天自增」，service 的 random 版是未对齐的遗留实现；自增从根上消除随机预占/碰撞，且序号连续对运维排查友好。

### 为什么选 R2 ensure
工单与 conversation 1:1，幂等 ensure 让「创建工单后确保有会话」成为调用方无脑操作，配合前端重试即可杜绝「工单存在无会话」。

## 影响范围
- `backend/case-service/app/services/case_service.py`：`create_case` 调用改为 repo 自增。
- `backend/conversation-service/app/routes/conversations.py`：create 接口幂等化。
- `frontend/customer/src/stores/chat.ts`：`createConversation` 复用幂等 create；`loadConversationHistory` 增加 404 降级。

## API 兼容性
- `POST /api/conversations/` 行为变更：重复 case_id 由 409/报错改为返回已有 conversation（幂等）。属健壮性改进，前端已适配。

## 实施计划
1. T1：case_id 自增（删 random，改 create_case）。
2. T2：conversation 幂等 + 前端 createConversation 重试。
3. T3：前端 404 降级。

## 风险与缓解
| 风险 | 影响 | 概率 | 缓解 |
|------|------|------|------|
| 自增序号跳变致旧工单与新工单序号不连续 | 低 | 必然 | 仅美观问题，不影响唯一性 |
| ensure 改 create 语义影响其他调用方 | 中 | 低 | code review + 单测覆盖 |

## 测试策略
- 单元：case_id 自增连续；conversation ensure 幂等返回同一 ID。
- 集成：创建工单 → 自动有会话；访问不存在工单 → 友好提示。
