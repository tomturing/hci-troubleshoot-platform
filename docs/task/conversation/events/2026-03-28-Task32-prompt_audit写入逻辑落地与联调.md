---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 32
---

# Task 32：prompt_audit 写入逻辑落地与联调（P2）

```
你是一名负责 hci-troubleshoot-platform conversation-service / case-service 联调的后端开发 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
根据 docs/16_评分机制与评价系统.md 与 docs/19_任务编排.md（Task 31）的设计：
- 数据库迁移 migrate_evaluation_v1.sql 已创建 prompt_audit 表
- case-service 与 conversation-service 的 QualityScoreService 均已实现「从 prompt_audit 读取元数据」的评分逻辑

但当前生产/测试库中：
- prompt_audit.has_sop / kb_chunks_count / kb_top_score 大量为 NULL
- 仅 messages / user_rating / payload_ref 等字段按设计允许为 NULL（采样或用户未评分）

这说明 Task 31 描述的「conversation-service 写入 prompt_audit」实现尚未真正落地，导致 ai_quality 维度长期缺失。

【任务目标】
在不改变既有评分算法前提下，**完全集成并验证 prompt_audit 写入链路**：
1. 按 docs/19_任务编排.md 中 Task 31 的设计，补全 conversation-service 写入 prompt_audit 的代码与单测
2. 联调 case-service / conversation-service，使 QualityScoreService 在绝大多数 case 上都能拿到非 NULL 的 has_sop / kb_chunks_count / kb_top_score
3. 通过一次端到端对话 → 关闭工单 → 查看评分 的流程，确认 ai_quality 维度已经参与计算

【涉及服务 / 文件范围】
- backend/conversation-service/app/services/conversation_service.py
- backend/conversation-service/app/repositories/conversation_repo.py
- backend/conversation-service/tests/unit/test_prompt_audit.py（新建）
- backend/case-service/app/services/quality_score.py（仅允许做必要的小幅兼容性调整）

【详细实现步骤】

Step 1：对齐设计文档与当前代码
- 仔细阅读：
  - docs/16_评分机制与评价系统.md（prompt_audit 设计 + 评分算法）
  - docs/19_任务编排.md 中 Task 31 的完整说明
  - database/migrate_evaluation_v1.sql 中 prompt_audit DDL
- 对比 backend/conversation-service 现有代码，确认：
  - 是否已存在 _build_system_prompt / send_message_stream_only 等入口
  - 是否还没有任何 insert_prompt_audit 调用（grep "prompt_audit" 应仅出现在文档或 case-service 中）

Step 2：在 conversation-service 中实现 prompt_audit 元数据采集
1. 修改 _build_system_prompt：
   - 签名从：
       async def _build_system_prompt(self, query: str, case_id: str) -> str:
     修改为：
       async def _build_system_prompt(
           self, query: str, case_id: str
       ) -> tuple[str, dict]:
   - 返回：
       - system_prompt: str  → 原有 4-Tier prompt 字符串
       - audit_meta: dict → {"has_sop": bool, "kb_chunks_count": int, "kb_top_score": float | None}
   - 在 sop_node / kb_chunks 处理完成后构造 audit_meta：
       audit_meta = {
           "has_sop": sop_node is not None and not isinstance(sop_node, Exception),
           "kb_chunks_count": len(kb_chunks) if isinstance(kb_chunks, list) else 0,
           "kb_top_score": (
               max((c.get("score", 0.0) for c in kb_chunks), default=None)
               if isinstance(kb_chunks, list) and kb_chunks else None
           ),
       }
   - KB 未启用或提前 return 的分支，同样返回 audit_meta：
       {"has_sop": False, "kb_chunks_count": 0, "kb_top_score": None}

2. 在 send_message_stream_only（或等效发送消息的 service 方法）中接收 audit_meta：
   - 从：
       system_prompt = await self._build_system_prompt(content, case_id)
     改为：
       system_prompt, _audit_meta = await self._build_system_prompt(content, case_id)

Step 3：实现 prompt_audit 写入后台任务
1. 在 send_message_stream_only 内，history_messages / all_messages 构建完成、resolved_assistant_type 已确定之后：
   - 以 ~10% 概率决定是否采样完整 messages payload：
       import random
       _do_sample = random.random() < 0.10
       _sample_payload = history_messages if _do_sample else None
2. 新增私有方法 _write_prompt_audit：
   - 签名：
       async def _write_prompt_audit(
           self,
           conversation_id: uuid.UUID,
           case_id: str,
           assistant_type: str,
           trace_id: str,
           message_count: int,
           audit_meta: dict,
           sample_payload: list | None,
       ) -> None:
   - 实现：
       - 使用 self.session_factory() 创建独立 AsyncSession
       - 调用 ConversationRepository.insert_prompt_audit(...) 插入一条记录
       - commit 成功后记录 event="prompt_audit_written" 的 info 日志
       - 捕获异常时仅记录 event="prompt_audit_write_error" 的 warning，不抛出到主流程
3. 在主流程中以 asyncio.create_task(...) fire-and-forget 触发写入：
   - 前置条件：self.session_factory 存在
   - 参数中填入：
       - conversation_id / case_id / resolved_assistant_type / trace_id
       - message_count=len(all_messages)
       - audit_meta=_audit_meta
       - sample_payload=_sample_payload

Step 4：在 ConversationRepository 中新增 insert_prompt_audit
1. 文件：backend/conversation-service/app/repositories/conversation_repo.py
2. 新增方法：
   async def insert_prompt_audit(
       self,
       conversation_id: uuid.UUID,
       case_id: str,
       assistant_type: str,
       trace_id: str,
       message_count: int,
       has_sop: bool,
       kb_chunks_count: int,
       kb_top_score: float | None,
       messages: list | None,
   ) -> None:
   - 使用 SQLAlchemy text(...) + self.session.execute(...) 写入 prompt_audit：
       INSERT INTO prompt_audit (
           conversation_id, case_id, assistant_type, trace_id,
           message_count, has_sop, kb_chunks_count, kb_top_score,
           messages
       ) VALUES (...)
   - messages 为 None 时插入 NULL；否则转为 JSON 字符串

Step 5：单元测试与端到端验证
1. 新建 backend/conversation-service/tests/unit/test_prompt_audit.py，至少包含：
   - test_build_system_prompt_returns_audit_meta_with_kb
   - test_build_system_prompt_returns_audit_meta_without_kb
   - test_prompt_audit_written_on_send_message
2. 本地运行：
   - uv run pytest backend/conversation-service/tests/unit/test_prompt_audit.py -v
3. 启动完整开发环境（make dev-up 或 docker compose），通过一次完整对话 + 关闭工单流程：
   - 发送若干条对话消息
   - 关闭工单触发 case-service 评分
   - 使用 psql/GUI 验证：
       SELECT has_sop, kb_chunks_count, kb_top_score, messages
       FROM prompt_audit
       ORDER BY captured_at DESC
       LIMIT 20;
   - 期望现象：
       - has_sop / kb_chunks_count / kb_top_score 基本不再是 NULL（未启用 KB 时为 False/0/NULL）
       - messages 有约 10% 的行非 NULL

Step 6：质量评分联调确认
1. 在 case-service / conversation-service 分别开启日志，关注：
   - event="prompt_audit_written"
   - event="quality_score_computed"
2. 对比同一 case_id：
   - prompt_audit 中 has_sop / kb_chunks_count / kb_top_score 已有值
   - assistant_evaluation 中 composite_score / score_breakdown 中的 ai_quality 维度不再被跳过

【约束】
- 不修改 prompt_audit 表结构（DDL 已在 migrate_evaluation_v1.sql 固化）
- 不改动 16_评分机制与评价系统.md 中已确认的评分公式与权重
- 所有新增注释使用中文
- 异步后台任务失败不得影响 AI 流式回复主链路

【验收标准】
- [ ] uv run pytest backend/conversation-service/tests/unit/test_prompt_audit.py -v 全部通过
- [ ] 连续发送 20 条 AI 对话消息后，SELECT COUNT(*) FROM prompt_audit = 20（每轮一条）
- [ ] SELECT COUNT(*) FROM prompt_audit WHERE messages IS NOT NULL ≈ 总数 10%（±5% 可接受）
- [ ] 随机抽查若干条 prompt_audit，has_sop / kb_chunks_count 不为 NULL（仅在 KB 未启用时为 False/0）
- [ ] 手动关闭至少 1 个工单后，assistant_evaluation.composite_score 存在，且 score_breakdown 中包含 ai_quality 维度
```

---