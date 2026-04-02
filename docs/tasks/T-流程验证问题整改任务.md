# 流程验证问题整改任务

> 创建日期：2026-03-31
> 最后更新：2026-04-02（合入云端 #88 后：T-FIX-02 范围缩小为 BUG-06 写回；T-FIX-05 移除已修复的 BUG-01；T-FIX-04 本地实现完成）
> 来源：[S-AI交互全流程验证方案.md](./S-AI交互全流程验证方案.md) 全流程演练中发现的 8 个问题
> 优先级说明：P0 = 主流程阻塞；P1 = 数据完整性；P2 = 可观测性补全

---

## Task T-FIX-01：REACT_ENABLED 环境变量注入，激活 ReactExecutor（P0）

```
你是一名负责 hci-troubleshoot-platform 部署配置层的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
全流程验证（CP-02、CP-06）发现：REACT_ENABLED 在 config.py 中默认为 False，
当前 Helm chart 的 conversation-service deployment.yaml 完全未注入该环境变量。
导致 ReactExecutor 从未被初始化，所有工具调用（SCP + acli）均不会发生，
对话退化为普通流式回复，tool_audit_log 中无任何记录。

本任务与 T31 重叠的部分：T31 聚焦 SCP_BASE_URL / SCP_API_KEY / SSH 密钥注入；
本任务聚焦 REACT_ENABLED 开关本身（最小可激活改动）。

相关文件：
- backend/conversation-service/app/config.py（Settings.REACT_ENABLED 字段）
- backend/conversation-service/app/main.py（第 96 行 if settings.REACT_ENABLED）
- deploy/helm/hci-platform/templates/conversation-service/deployment.yaml
- hci-platform-env/environments/dev/values.yaml（dev 环境实际注入值）

【任务目标】
1. 确认 config.py 中 REACT_ENABLED 的默认值逻辑合理（默认 False 正确，需外部注入为 True）
2. 在 Helm deployment.yaml 的 env 区块添加 REACT_ENABLED 环境变量注入（从 .Values 读取）
3. 在 hci-platform-env dev/values.yaml 中添加对应配置项，默认 true（dev 先开）
4. 验证：dev 环境重启 conversation-service 后，日志出现 ReactExecutor 初始化成功的条目

【涉及服务 / 文件范围】
- backend/conversation-service/app/config.py（只读确认，不改）
- deploy/helm/hci-platform/templates/conversation-service/deployment.yaml
- hci-platform-env/environments/dev/values.yaml

【详细实现步骤】

Step 1：确认当前 config.py 中 REACT_ENABLED 声明位置
- 查找 REACT_ENABLED 字段定义，确认是否已有 env 读取逻辑
- 查找 main.py 中该开关的使用位置（ReactExecutor 初始化入口）

Step 2：修改 Helm deployment.yaml
- 在 conversation-service 的 env 列表追加：
  - name: REACT_ENABLED
    value: "{{ .Values.conversationService.reactEnabled }}"
- 对齐现有 DATABASE_URL 等变量的注入模式

Step 3：修改 hci-platform-env dev values.yaml
- 在 conversationService 块下添加：
  reactEnabled: "true"
- staging/prod 环境暂不开启（保持 false），需在对应 values.yaml 显式声明

Step 4：验证
- 执行 helm upgrade 或重启 pod
- 检查日志：
  {service="conversation-service"} | json | message=~"ReactExecutor"
- 预期日志：ReactExecutor 初始化成功 / REACT_ENABLED=true

【约束】
- 不修改 Python 业务代码
- 不改动 prod 环境配置
- 代码注释使用中文

【验收标准】
- [ ] dev 环境 conversation-service 启动日志含 ReactExecutor 初始化成功事件
- [ ] tool_audit_log 在发送第一条消息后出现 get_active_alerts / get_failed_tasks / get_vm_list 三条记录
- [ ] make lint 通过（Helm chart YAML 语法无误）
```

---

## Task T-FIX-02：补全阶段上下文写回（hypothesis / root_cause）（P0）

```
你是一名负责 hci-troubleshoot-platform conversation-service 对话质量的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
云端 PR #88（feat(s0): S0 意图识别与分类基线重构）已修复 BUG-05：
  _segment_methodology() 现在对模板字符串执行 .format(session_state)，
  占位符 {known_info}、{hypothesis}、{root_cause} 在技术上可以被替换。

但 BUG-06 仍未解决（全流程验证 CP-05、CP-06、CP-09 受影响）：
  S2 阶段 AI 生成假设列表后，该假设从未写回 conversation.metadata["hypothesis"]，
  导致 S3 阶段调用 _segment_methodology("S3", session_state) 时，
  session_state["hypothesis"] 为 []（空列表转字符串），
  LLM 在验证阶段看不到需要验证的假设。

  同理，S4 根因确认后 root_cause 未写回 conversation.metadata["root_cause"]，
  导致 S5 阶段 {root_cause} 被替换为 "待确认"，LLM 无法生成有针对性的方案。

相关文件：
- backend/conversation-service/app/services/conversation_manager.py
  （detect_stage_transition_with_category：负责阶段转换）
- backend/conversation-service/app/services/conversation_service.py
  （send_message_stream_only：调用链主逻辑）
- backend/conversation-service/app/services/prompt_builder.py
  （_segment_methodology：读取 session_state 拼 prompt）

【任务目标】
1. S2 阶段结束（AI 回复中出现假设列表）时，将假设文本写回 conversation.metadata["hypothesis"]
2. S4 根因确认时，将根因文本写回 conversation.metadata["root_cause"]
3. 确保 S3/S5 阶段构建 Prompt 时，对应字段有实际内容而非兜底空值

【涉及服务 / 文件范围】
- backend/conversation-service/app/services/conversation_service.py
- backend/conversation-service/app/services/conversation_manager.py
- backend/conversation-service/tests/

【详细实现步骤】

Step 1：确认现有 session_state 读取路径
- 阅读 conversation_service.py 中 _build_system_prompt 调用链
- 确认 session_state dict 是从 conversation.metadata 读取的哪些字段
- 确认当前 conversation.metadata 在什么时机被写入

Step 2：S2 → S3 写回 hypothesis
在 conversation_service.py 中，当 diagnostic_stage 从 S2 跳转到 S3 时
（_update_diagnostic_stage 调用后），提取当前 AI 回复（assistant_message 内容）：
  hypothesis_text = self._extract_hypothesis(assistant_content)
  await self._update_conversation_metadata(conv_id, {"hypothesis": hypothesis_text})

新建辅助函数 _extract_hypothesis(text: str) -> str：
  简单策略：提取含"假设"关键词的段落，或直接取 AI 回复全文（不需要精确解析）
  兜底：若未提取到，返回 "（S2 未生成假设）"

Step 3：S4 → S5 写回 root_cause
同理，当 diagnostic_stage 从 S4 跳转到 S5 时，提取根因确认文本：
  root_cause_text = self._extract_root_cause(assistant_content)
  await self._update_conversation_metadata(conv_id, {"root_cause": root_cause_text})

新建辅助函数 _extract_root_cause(text: str) -> str：
  策略：尝试提取"根因确认："之后的文本，兜底返回 AI 回复前 200 字

Step 4：_update_conversation_metadata 工具函数
新增（若不存在）：
  async def _update_conversation_metadata(self, conv_id: str, patch: dict) -> None:
      """将 patch 中的 key-value 合并写入 conversation.metadata，不覆盖已有字段"""
      async with self.session_factory() as session:
          conv = await session.get(Conversation, conv_id)
          if conv:
              current = conv.metadata or {}
              conv.metadata = {**current, **patch}
              await session.commit()

Step 5：单元测试
  test_hypothesis_written_on_s2_to_s3()    -- 跳转后 metadata["hypothesis"] 有内容
  test_root_cause_written_on_s4_to_s5()    -- 跳转后 metadata["root_cause"] 有内容
  test_s3_prompt_has_hypothesis_content()  -- S3 系统提示词不含 "（暂无假设）"

【约束】
- hypothesis 和 root_cause 的提取不需要精确结构化，字符串摘要即可
- 不修改 prompt_builder.py 中对 session_state 的读取逻辑（已由 #88 实现）
- 代码注释使用中文

【验收标准】
- [ ] uv run pytest backend/conversation-service/tests/ -k "hypothesis or root_cause" -q 全部通过
- [ ] 手动走完 S2 阶段，查询 DB：conversation.metadata 中含 hypothesis 键且值非空
- [ ] 手动走完 S4 阶段，查询 DB：conversation.metadata 中含 root_cause 键且值非空
- [ ] make lint 通过
```

---

## Task T-FIX-03：diagnostic_stage 内存与 DB 一致性保障（P1）

```
你是一名负责 hci-troubleshoot-platform conversation-service 状态管理的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
全流程验证（CP-03）发现：conversation_service.py 的 _update_diagnostic_stage 函数
使用独立 session 执行 DB UPDATE，但同时也更新了内存中 conv.diagnostic_stage（第 761 行）。

当独立 session 的 commit 失败时（网络抖动、DB 连接断开等），
内存中的 diagnostic_stage 已经被改写为新阶段，
而数据库中仍是旧阶段，导致：
  - 下一次请求读取内存对象时，用错误的阶段构建 Prompt
  - 服务重启后（内存清空），从 DB 读取的是旧阶段，产生状态回退
  - 日志中无法区分"DB 已提交"和"仅内存更新"

相关文件：
- backend/conversation-service/app/services/conversation_service.py
  （_update_diagnostic_stage 函数，约第 735-780 行）

【任务目标】
1. 修复内存更新应在 DB commit 成功后才执行
2. 在日志中明确区分"DB 提交成功"和"DB 提交失败（内存未同步）"两种情况
3. DB commit 失败时，记录 error 日志并保持内存对象不变（不更新内存）

【涉及服务 / 文件范围】
- backend/conversation-service/app/services/conversation_service.py
- backend/conversation-service/tests/test_conversation_service.py（或新建）

【详细实现步骤】

Step 1：阅读 _update_diagnostic_stage 的当前实现
- 确认独立 session 的 commit 调用位置
- 确认 conv.diagnostic_stage = new_stage 的位置（第 761 行附近）
- 确认 try/except 是否已存在及覆盖范围

Step 2：修改内存写入时机
将内存更新从 try 块移到 commit 成功后（finally 前、except 前）：
  try:
      async with self.session_factory() as session:
          await session.execute(UPDATE ... diagnostic_stage=new_stage)
          await session.commit()
      # commit 成功后才更新内存
      conv.diagnostic_stage = new_stage
      logger.info(event="diagnostic_stage_transition", db_committed=True, ...)
  except Exception as e:
      # commit 失败，内存不动
      logger.error(event="diagnostic_stage_update_error", db_committed=False, ...)

Step 3：增加日志字段
- 成功日志：增加 db_committed=True 字段
- 失败日志：增加 db_committed=False 字段，保留 error 信息

Step 4：单元测试
新增或补充 test_update_diagnostic_stage_db_fail_no_memory_update()：
  - mock session.commit() 抛出异常
  - 断言调用后 conv.diagnostic_stage 仍为旧值
  - 断言 logger.error 被调用一次

【约束】
- 不修改独立 session 的使用方式（保留独立事务策略）
- 不引入新的数据库层依赖
- 代码注释使用中文

【验收标准】
- [ ] uv run pytest backend/conversation-service/tests/ -k "diagnostic_stage" -q 全部通过
- [ ] 手动模拟 DB 断连，确认 Loki 日志输出 db_committed=False，内存阶段未变
- [ ] make lint 通过
```

---

## Task T-FIX-04：工具确认失败时补全审计日志（P1）

```
你是一名负责 hci-troubleshoot-platform conversation-service 审计合规的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
全流程验证（CP-07）发现：react_executor.py 的 _execute_tool_call 函数在处理
risk_level >= 2 的工具时，若 confirm_service.request_confirm() 返回 False
（用户取消或 120s 等待超时），当前返回值为一个普通错误字符串，
但 tool_audit_log 中该条记录的 error 字段为 NULL，authorized_by 字段也为 NULL。

这导致审计日志无法区分"用户主动拒绝"和"等待超时被取消"两种情况，
违反审计合规要求（高风险操作必须有完整的授权/拒绝记录）。

相关文件：
- backend/conversation-service/app/core/react_executor.py
  （_execute_tool_call 函数，confirm 检查段）
- database/migrate_tool_audit_log.sql（参考 tool_audit_log 表结构）

【任务目标】
1. confirm_service 返回 False 时，明确区分两种子情况并写入 tool_audit_log.error
2. 超时取消：error = "confirm_timeout"，authorized_by = "system-timeout"
3. 用户拒绝：error = "user_rejected"，authorized_by 记录操作用户 ID（若可获取）
4. 上述两种情况均需在 Loki 日志中有独立 event 字段

【涉及服务 / 文件范围】
- backend/conversation-service/app/core/react_executor.py
- backend/conversation-service/app/services/confirm_service.py（如存在）
- backend/conversation-service/tests/test_react_executor.py（或新建）

【详细实现步骤】

Step 1：阅读 confirm_service.request_confirm 的返回值约定
- 确认当前返回 bool 还是更丰富的结构（ConfirmResult 等）
- 若只返回 bool，评估是否需要改为返回枚举/命名元组区分超时与拒绝

Step 2：修改 confirm_service 返回值（若需要）
推荐修改为返回枚举：
  class ConfirmResult(str, Enum):
      APPROVED = "approved"
      REJECTED = "rejected"
      TIMEOUT  = "timeout"
若修改接口，同步更新 ConfirmServiceProtocol 中的 Protocol 定义。

Step 3：修改 _execute_tool_call 中的审计写入逻辑
在 confirm 返回 False 分支：
  if result == ConfirmResult.TIMEOUT:
      error_msg = "confirm_timeout"
      authorized_by = "system-timeout"
  else:  # REJECTED
      error_msg = "user_rejected"
      authorized_by = user_id_from_context  # 从 state 或 session 中获取
  # 写入 tool_audit_log
  await self.audit.write(
      audit_id, ..., error=error_msg, authorized_by=authorized_by
  )

Step 4：补充日志事件
  logger.warning(
      event="tool_confirm_declined",
      reason=error_msg,
      tool_name=tool_call.name,
      session_id=state.session_id,
  )

Step 5：单元测试
  test_tool_confirm_timeout_audit_log()   -- 超时时 error="confirm_timeout", authorized_by="system-timeout"
  test_tool_confirm_rejected_audit_log()  -- 拒绝时 error="user_rejected"

【约束】
- 若 ConfirmServiceProtocol 接口改动，必须同步更新所有实现类（包括 mock）
- 不修改 tool_audit_log 表结构（error 字段已为 TEXT，authorized_by 已为 VARCHAR，可直接使用）
- 代码注释使用中文

【验收标准】
- [ ] uv run pytest backend/conversation-service/tests/ -k "confirm" -q 全部通过
- [ ] 手动触发超时场景（等待 120s 或 mock），DB 中对应记录 error="confirm_timeout"
- [ ] make lint 通过
```

---

## Task T-FIX-05：可观测性补全（step_no + prompt_audit_meta）（P2）

```
你是一名负责 hci-troubleshoot-platform 可观测性补全的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
全流程验证发现两处可观测性盲点（BUG-03 / BUG-08）：

注意：BUG-01（case.category 前端不传导致分类缺失）已由云端 PR #88 修复：
  S0 阶段 AI 通过故障分类确认流程，自动将 category_id/category_l1/category_l2
  写入 conversation 表，不再依赖前端传入 case.category，本任务无需处理 BUG-01。

BUG-03：tool_audit_log 缺少 step_no 字段，
  同一个 session 内多轮 ReAct 循环的工具调用无法区分顺序，
  排查问题时难以还原执行路径。
  期望：新增 step_no INTEGER 字段，由 ReactExecutor 传入当前步骤号。

BUG-08：_build_system_prompt / KnowledgeRetriever 返回的 audit_meta
（包含 has_sop / kb_chunks_count / kb_top_score / fallback_level）
只存在于内存，未持久化，无法事后回溯 RAG 命中情况。
  期望：将 audit_meta 写入对应 message 的 metadata 字段。

相关文件：
- backend/conversation-service/app/core/react_executor.py（BUG-03）
- database/（新增 migration 文件，BUG-03）
- backend/conversation-service/app/services/conversation_service.py（BUG-08）
- backend/conversation-service/app/repositories/message_repo.py（BUG-08）

【任务目标】
1. BUG-03：tool_audit_log 新增 step_no 字段，ReactExecutor 传入当前步骤号
2. BUG-08：_build_system_prompt 返回的 audit_meta 写入 message.metadata

【涉及服务 / 文件范围】
- backend/conversation-service/app/core/react_executor.py
- backend/conversation-service/app/services/conversation_service.py
- database/（新增 migration SQL）
- backend/conversation-service/tests/（单元测试）

【详细实现步骤】

Step 1：BUG-03 — tool_audit_log 新增 step_no 字段
- 新建 database/migrate_tool_audit_log_step_no.sql：
  ALTER TABLE tool_audit_log ADD COLUMN IF NOT EXISTS step_no INTEGER;
  CREATE INDEX IF NOT EXISTS idx_tool_audit_log_step_no
      ON tool_audit_log(session_id, step_no);
  COMMENT ON COLUMN tool_audit_log.step_no IS 'ReAct 推理步骤序号（state.step_count）';
- 修改 react_executor.py 的 audit.write() 调用，传入 step_no=state.step_count

Step 2：BUG-08 — audit_meta 写入 message.metadata
在 conversation_service.py 的 send_message_stream_only 中：
  system_prompt, audit_meta = await self._build_system_prompt(...)
AI 回复保存时，在 message.metadata 中追加 audit_meta：
  metadata = {
      "audit": {
          "has_sop": audit_meta.get("has_sop"),
          "kb_chunks_count": audit_meta.get("kb_chunks_count"),
          "kb_top_score": audit_meta.get("kb_top_score"),
          "fallback_level": audit_meta.get("fallback_level"),
      }
  }

Step 3：单元测试
  test_step_no_in_audit_log()        -- step_no 正确写入
  test_audit_meta_in_message_metadata() -- message.metadata 含 audit.fallback_level

【约束】
- BUG-03 的 DB migration 使用 ADD COLUMN IF NOT EXISTS，向后兼容
- 代码注释使用中文

【验收标准】
- [ ] tool_audit_log 中每条工具调用记录的 step_no 与 ReAct 步骤号一致
- [ ] message.metadata 中含 audit.fallback_level 字段
- [ ] uv run pytest backend/ -q 全部通过
- [ ] make lint 通过
```
