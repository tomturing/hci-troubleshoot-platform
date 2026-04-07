---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 31
---

# Task 31：prompt_audit 写入逻辑（P2）

```
你是一名负责 hci-troubleshoot-platform conversation-service 开发的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Task 30-A 的数据库迁移（migrate_evaluation_v1.sql）已建好 prompt_audit 表，
但 conversation-service 从未向该表写入数据，导致 ai_quality 评分维度（权重 15%）
在大多数 case 计算时被跳过（has_sop/kb_chunks_count 始终为 None）。

当前代码位置：
  backend/conversation-service/app/services/conversation_service.py

问题根因：
  _build_system_prompt(query, case_id) 方法内部获取了 sop_node 和 kb_chunks，
  但只返回了 system_prompt 字符串，这些关键元数据没有被持久化。

【任务目标】
修改 conversation_service.py，在每轮 AI 对话构建 4-Tier Prompt 后，
将以下元数据异步写入 prompt_audit 表（100% 覆盖）：
  - has_sop    : sop_node is not None
  - kb_chunks_count : len(kb_chunks)
  - kb_top_score    : max(c.get("score", 0) for c in kb_chunks) 若有，否则 None
  - conversation_id / case_id / assistant_type / trace_id / message_count
  - messages (完整 payload) : 按 10% 概率采样存储，其余为 None

【涉及文件】
  backend/conversation-service/app/services/conversation_service.py   # 主改动
  backend/conversation-service/app/repositories/conversation_repo.py  # 新增写入方法

【详细设计】

Step 1: 修改 _build_system_prompt 的返回值

  当前签名：
    async def _build_system_prompt(self, query: str, case_id: str) -> str:

  修改为返回 tuple：
    async def _build_system_prompt(
        self, query: str, case_id: str
    ) -> tuple[str, dict]:
        """
        返回：
          [0] system_prompt 字符串（原有）
          [1] audit_meta 字典，包含：
              {
                "has_sop": bool,
                "kb_chunks_count": int,
                "kb_top_score": float | None,
              }
        """

  在方法内部，在现有的 sop_node / kb_chunks 处理完成后，
  构建 audit_meta 并随 prompt 一起返回：

    audit_meta = {
        "has_sop": sop_node is not None and not isinstance(sop_node, Exception),
        "kb_chunks_count": len(kb_chunks) if isinstance(kb_chunks, list) else 0,
        "kb_top_score": (
            max((c.get("score", 0.0) for c in kb_chunks), default=None)
            if isinstance(kb_chunks, list) and kb_chunks else None
        ),
    }
    return "\n\n".join(sections), audit_meta

  注意：KB 未启用时（早期返回分支）也需要返回 audit_meta：
    return _SYSTEM_BASE + f"\n\n---\n当前工单 ID：{case_id}", {
        "has_sop": False,
        "kb_chunks_count": 0,
        "kb_top_score": None,
    }

Step 2: 在 send_message_stream_only 中捕获 audit_meta

  修改调用处（发现在函数中搜索 "构建 4-Tier System Prompt" 注释行下方）：

    # 修改前：
    system_prompt = await self._build_system_prompt(content, case_id)

    # 修改后：
    system_prompt, _audit_meta = await self._build_system_prompt(content, case_id)

Step 3: 写入 prompt_audit（fire-and-forget 后台任务）

  在 Step 2 之后，发起后台写入任务：

    # 3.x 写入 prompt_audit 元数据（异步，不阻塞流式回复）
    import random
    _sample_payload = None
    if random.random() < 0.10:  # 10% 概率采样完整 payload（在 history_messages 构建完成后设置）
        # history_messages 在 Step 3（原步骤编号）构建，此处先记录标记，
        # 在 history_messages 构建后填充（见下文 Step 3b）
        _do_sample = True
    else:
        _do_sample = False

  注意：payload 采样需在 history_messages 构建完成后才能进行。
  因此，在 history_messages 构建完成后（原来的 Step 3 获取历史上下文结束后 ），
  才触发后台任务：

    # 构建完 history_messages 之后
    _sample_payload = history_messages if _do_sample else None

    asyncio.create_task(
        self._write_prompt_audit(
            conversation_id=conversation_id,
            case_id=case_id,
            assistant_type=resolved_assistant_type,  # 注意：此时 resolved_assistant_type 可能还未确定
            trace_id=trace_id,
            message_count=len(all_messages),
            audit_meta=_audit_meta,
            sample_payload=_sample_payload,
        )
    )

  【重要】 resolved_assistant_type 在 Step 4（从注册表获取AI助手）才确定，
  而 history_messages 在 Step 3 构建。建议在 Step 4 之后（ai_client 确定后）
  才触发 prompt_audit 后台任务，此时 resolved_assistant_type 已知。

  最终插入时机（伪代码）：

    # 4. 从注册表获取AI助手客户端（原有代码不变）
    resolved_assistant_type = await self._resolve_assistant_type(conversation_id, assistant_type)
    ai_client = ...

    # 4.x 写入 prompt_audit（fire-and-forget）
    _sample_payload = history_messages if _do_sample else None
    if self.session_factory:
        asyncio.create_task(
            self._write_prompt_audit(
                conversation_id=conversation_id,
                case_id=case_id,
                assistant_type=resolved_assistant_type,
                trace_id=trace_id,
                message_count=len(all_messages),
                audit_meta=_audit_meta,
                sample_payload=_sample_payload,
            )
        )

Step 4: 新增 _write_prompt_audit 私有方法

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
      """写入 prompt_audit 记录（后台任务，失败不影响主流程）"""
      try:
          async with self.session_factory() as session:
              await ConversationRepository(session).insert_prompt_audit(
                  conversation_id=conversation_id,
                  case_id=case_id,
                  assistant_type=assistant_type,
                  trace_id=trace_id,
                  message_count=message_count,
                  has_sop=audit_meta["has_sop"],
                  kb_chunks_count=audit_meta["kb_chunks_count"],
                  kb_top_score=audit_meta["kb_top_score"],
                  messages=sample_payload,
              )
              await session.commit()
          logger.info(
              event="prompt_audit_written",
              conversation_id=str(conversation_id),
              case_id=case_id,
              has_sop=audit_meta["has_sop"],
              kb_chunks_count=audit_meta["kb_chunks_count"],
              sampled=sample_payload is not None,
          )
      except Exception as e:
          # 审计失败不影响业务流程，只记录 warning
          logger.warning(
              event="prompt_audit_write_error",
              message=str(e),
              conversation_id=str(conversation_id),
          )

Step 5: 在 ConversationRepository 新增 insert_prompt_audit 方法

  文件：backend/conversation-service/app/repositories/conversation_repo.py

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
      """向 prompt_audit 表插入一条审计记录"""
      import json as _json
      await self.session.execute(
          text("""
              INSERT INTO prompt_audit (
                  conversation_id, case_id, assistant_type, trace_id,
                  message_count, has_sop, kb_chunks_count, kb_top_score,
                  messages
              ) VALUES (
                  :conversation_id, :case_id, :assistant_type, :trace_id,
                  :message_count, :has_sop, :kb_chunks_count, :kb_top_score,
                  :messages
              )
          """),
          {
              "conversation_id": str(conversation_id),
              "case_id": case_id,
              "assistant_type": assistant_type,
              "trace_id": trace_id,
              "message_count": message_count,
              "has_sop": has_sop,
              "kb_chunks_count": kb_chunks_count,
              "kb_top_score": kb_top_score,
              "messages": _json.dumps(messages, ensure_ascii=False) if messages else None,
          },
      )

【单元测试】

新增文件：backend/conversation-service/tests/unit/test_prompt_audit.py

测试用例：

1. test_build_system_prompt_returns_audit_meta_with_kb
   - Mock kb_client.sop_match → 返回 {"title": "SOP-001", "content": "..."}
   - Mock kb_client.search → 返回 [{"content": "chunk1", "score": 0.85}]
   - 调用 _build_system_prompt("磁盘IO异常", "Q001")
   - 断言 audit_meta["has_sop"] == True
   - 断言 audit_meta["kb_chunks_count"] == 1
   - 断言 audit_meta["kb_top_score"] == 0.85

2. test_build_system_prompt_returns_audit_meta_without_kb
   - KB 未启用（settings.KB_ENABLED = False）
   - 断言 audit_meta["has_sop"] == False
   - 断言 audit_meta["kb_chunks_count"] == 0

3. test_prompt_audit_written_on_send_message
   - Mock session_factory（使用 AsyncMock）
   - 调用 send_message_stream_only(conversation_id, case_id, "测试消息")
   - 断言 ConversationRepository.insert_prompt_audit 被调用了一次
   - 断言调用参数中 has_sop 字段类型为 bool，kb_chunks_count 类型为 int

【验收标准】
- [ ] 发送一条 AI 对话消息后，psql -c "SELECT has_sop, kb_chunks_count FROM prompt_audit ORDER BY created_at DESC LIMIT 1" 有记录，has_sop / kb_chunks_count 非 NULL
- [ ] 连续发送 20 条消息，SELECT COUNT(*) FROM prompt_audit 数量 = 20（100% 写入）
- [ ] SELECT COUNT(*) FROM prompt_audit WHERE messages IS NOT NULL 约为总数 10%（采样验证，±5% 可接受）
- [ ] 全部 3 个单元测试通过：uv run pytest backend/conversation-service/tests/unit/test_prompt_audit.py -v
- [ ] 无 prompt_audit 写入失败日志（conversation-service 日志中无 event=prompt_audit_write_error）

【约束】
- 后台任务（asyncio.create_task）失败只记录 warning，不影响 AI 流式回复主流程
- 采样率 10% 已硬编码，不需要外部配置项（可后续迭代）
- 不修改与本任务无关的代码
- 代码注释使用中文
- Python 环境管理使用 uv

完成后提交 PR，描述中包含：psql 查询截图（prompt_audit 有数据）、单测通过截图。
```
# 20_任务编排（prompt_audit 与评分体系收尾）

> 本文件用于补齐 prompt_audit / 质量评分相关的「设计已完成但实现缺口」部分，聚焦落地与验收。
> 优先级说明：P1 = 生产环境完整性；P2 = 功能完善；P3 = 数据与观测优化  
> 创建日期: 2026-03-13

---