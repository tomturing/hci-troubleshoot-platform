---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 08
---

# Task 08：诊断状态机——ConversationManager + PromptBuilder（P1）

```
你是一名负责 hci-troubleshoot-platform conversation-service 的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Task 07 在数据库层添加了 diagnostic_stage 等状态字段。
本任务在应用层实现诊断状态机，让对话能按 S0→S1→S2→S3→S4 推进，
而不是每轮独立对话。

HCI 排障的 7 个诊断阶段（S0-S6）：
  S0：意图识别 — 提取关键实体，查看告警/任务日志，提出 1-3 个确认问题
  S1：故障定位 — 根据用户回答，确认故障分类（定位到 category_baseline 叶节点）
  S2：假设生成 — 生成 2-3 个根因假设，按概率排序
  S3：验证执行 — 逐一执行诊断命令（工具调用），收集证据
  S4：根因确认 — 基于证据确定根因
  S5：方案输出 — 输出明确可执行的修复步骤
  S6：验证闭环 — 用户确认问题解决，记录知识

状态转换规则（由 GLM 在每轮回复后判断）：
  S0 → S1：用户确认了故障分类
  S1 → S2：定位到具体 category_id
  S2 → S3：开始执行第一个诊断命令
  S3 → S4：诊断命令执行完毕，有足够证据
  S4 → S5：确定根因
  S5 → S6：用户执行了修复步骤
  任何阶段 → S0：用户说"重新来" / 问题描述发生根本变化

PromptBuilder 设计原则：
  - Segment 2（诊断方法论）随 diagnostic_stage 变化
  - S3 阶段注入可用工具列表（为 Phase 3 ReAct 工具调用预热）
  - 知识原子的注入逻辑：优先注入 diagnostic_step 类型，其次 fix_action

【任务目标】
1. 实现 ConversationManager 状态机（S0-S6 转换逻辑）
2. 将 Task 01 的 Prompt 片段组织为完整的 PromptBuilder 类
3. PromptBuilder 能根据 diagnostic_stage 动态调整 Segment 2 内容
4. 每轮对话结束后，更新 DB 中的 diagnostic_stage
5. 验证：发 3 轮消息后，diagnostic_stage 能从 S0 推进到 S1 再到 S2

【涉及服务 / 文件范围】
允许修改/新建：
  - backend/conversation-service/app/services/conversation_service.py（主服务）
  - backend/conversation-service/app/services/prompt_builder.py（新建）
  - backend/conversation-service/app/services/conversation_manager.py（新建）
只读参考：
  - docs/architecture/各层最优设计.md § Layer 2（5段式 Prompt）、Layer 6
  - docs/architecture/完整技术方案.md § 五、Phase 2
  - data-pipeline/config/category_baseline.yaml（198 个分类）
  - Task 01 已改写的 _SYSTEM_BASE（沿用其 Prompt 片段）

【详细实现步骤】

Step 1：新建 prompt_builder.py

```python
# backend/conversation-service/app/services/prompt_builder.py
"""5 段式动态 Prompt 构建器，随诊断阶段变化"""

# 各阶段的 Segment 2 内容
STAGE_METHODOLOGY = {
    "S0": """当前阶段：【S0 意图识别】
你的任务：
1. 从客户描述中提取关键实体（虚拟机名/集群名/存储名/时间点）
2. 根据实体信息，结合告警日志和操作日志，判断故障的大方向
3. 提出 1-3 个精准确认问题，不要一次问超过 3 个
4. 参考分类基准，尝试初步定位到技术域（虚拟机/存储/网络/硬件/平台）""",

    "S1": """当前阶段：【S1 故障定位】
已有信息：{known_info}
你的任务：
1. 根据客户回答，确认具体故障类型
2. 尝试精确到 category_baseline 中的叶节点（如：虚拟机开机失败）
3. 如信息不足，继续追问，但每轮不超过 2 个问题""",

    "S2": """当前阶段：【S2 假设生成】
故障分类：{category_path}
你的任务：
1. 列出 2-3 个最可能的根因假设，每个假设给出可能性百分比
2. 说明每个假设的诊断方向（需要检查什么）
3. 优先排查概率最高的假设""",

    "S3": """当前阶段：【S3 验证执行】
故障分类：{category_path}
当前假设：{hypothesis}
你的任务：
1. 按假设概率从高到低，逐一调用诊断工具收集证据
2. 每次工具调用后，根据结果更新假设状态
3. 若当前假设被排除，检验下一个假设
4. 收集到决定性证据后，终止验证，进入 S4""",

    "S4": """当前阶段：【S4 根因确认】
你的任务：
1. 综合所有诊断证据，明确说明根本原因
2. 用"根因确认：XXX" 格式表述
3. 如果证据不足以确定根因，说明还需要什么信息""",

    "S5": """当前阶段：【S5 方案输出】
根因：{root_cause}
你的任务：
1. 输出明确的修复步骤，每步有可执行命令或操作说明
2. 区分"快速恢复方案"（临时解决）和"彻底解决方案"
3. 注明操作风险（是否需要停机/迁移虚拟机）
4. 提示用户执行完成后回复确认""",

    "S6": """当前阶段：【S6 验证闭环】
你的任务：
1. 询问用户是否确认问题已解决
2. 如已解决，说明本次排障的总结（耗时、根因、方案）
3. 如未解决，分析可能原因，考虑是否需要上升支持""",
}

class PromptBuilder:
    """根据诊断阶段和上下文动态构建 5 段式 Prompt"""

    def build_system_prompt(
        self,
        diagnostic_stage: str,
        knowledge_atoms: list[dict],
        case_context: dict,
        session_state: dict,
    ) -> str:
        """
        构建完整 system prompt
        
        参数：
          diagnostic_stage: 当前诊断阶段 S0-S6
          knowledge_atoms: KB 检索结果（知识原子列表）
          case_context: 工单上下文 {case_id, description}
          session_state: 会话状态 {category_path, hypothesis, root_cause 等}
        """
        segments = [
            self._segment_identity(),
            self._segment_methodology(diagnostic_stage, session_state),
            self._segment_mechanism_knowledge(),
            self._segment_knowledge_atoms(knowledge_atoms),
            self._segment_context(case_context, diagnostic_stage),
        ]
        return "\n\n".join(filter(None, segments))

    def _segment_identity(self) -> str:
        return """你是深信服超融合基础设施（HCI）智能排障专家助手。
你掌握完整的 HCI 平台知识：虚拟机生命周期管理、分布式存储 ASAN、
vxlan 虚拟网络、IPMI 硬件管理、acli 诊断工具集（完整命令集）。
你的目标是协助现场工程师快速、精准地定位和解决 HCI 平台故障。"""

    def _segment_methodology(self, stage: str, state: dict) -> str:
        template = STAGE_METHODOLOGY.get(stage, STAGE_METHODOLOGY["S0"])
        # 替换模板占位符
        return template.format(
            known_info=state.get('known_info', '暂无'),
            category_path=' > '.join(filter(None, [
                state.get('category_l1'), state.get('category_l2')
            ])) or '待定位',
            hypothesis=str(state.get('hypothesis', [])),
            root_cause=state.get('root_cause', '待确认'),
        )

    def _segment_mechanism_knowledge(self) -> str:
        """硬编码的 HCI 机制知识（不依赖 RAG）"""
        return """【HCI 核心机制知识】
虚拟机开机链路：用户触发 → vtpdaemon → kvm_runner → QEMU/KVM → 存储挂载 → 网络配置
开机失败 4 大方向：① 宿主资源不足（CPU/内存）② 存储不可访问
                  ③ 序列号/授权问题 ④ 平台服务异常（exporter/cfs/prometheus）
诊断首选命令：acli task get -v {vmid} -k '启动虚拟机' → 获取失败原因
快速预诊断：acli alert get -l 10 → 近期告警，acli task get -s failed -l 5 → 近期失败任务"""

    def _segment_knowledge_atoms(self, atoms: list[dict]) -> str:
        if not atoms:
            return """【知识库参考】
当前知识库暂无该类型故障的 SOP，将基于 HCI 机制知识进行推理。
所有机制推理内容将标注【机制推理】，与知识库匹配内容（【KB参考】）区分。"""

        atom_texts = []
        for atom in atoms[:5]:   # 最多注入 5 个知识原子
            content = atom.get('content', {})
            full_text = content.get('full_text', '')[:500]  # 限制长度
            commands = content.get('commands', [])
            atom_type = atom.get('type', '')
            source = atom.get('source_ref', '知识库')

            label = {
                'diagnostic_step': '诊断步骤',
                'fix_action': '解决方案',
                'decision_gate': '判断条件',
                'background': '背景知识',
            }.get(atom_type, '参考')

            cmd_text = '\n'.join(f'  $ {c}' for c in commands[:3]) if commands else ''
            atom_texts.append(
                f"【KB参考 - {label}】[来源: {source}]\n{full_text}"
                + (f"\n关键命令：\n{cmd_text}" if cmd_text else "")
            )

        return "【知识库参考资料】\n\n" + "\n\n---\n".join(atom_texts)

    def _segment_context(self, ctx: dict, stage: str) -> str:
        return f"""【当前工单上下文】
工单 ID：{ctx.get('case_id', '未知')}
客户描述：{ctx.get('description', '（待获取）')}
当前诊断阶段：{stage}
请直接开始{{'S0': '意图确认', 'S1': '故障定位', 'S2': '假设生成',
              'S3': '诊断验证', 'S4': '根因确认', 'S5': '输出方案',
              'S6': '验证闭环'}.get(stage, '下一步')}"""
```

Step 2：新建 conversation_manager.py

```python
# backend/conversation-service/app/services/conversation_manager.py
"""会话诊断状态管理器：维护 S0-S6 状态转换"""
import re

# 阶段转换触发词（从 GLM 回复中识别）
STAGE_TRIGGERS = {
    ("S0", "S1"): [
        r"确认是.*(?:虚拟机|存储|网络|主机|告警)",
        r"(?:故障|问题).*(?:定位|确认)为",
    ],
    ("S1", "S2"): [
        r"(?:故障分类|问题类型).*(?:确定|明确)",
        r"开始分析(?:可能)?根因",
    ],
    ("S2", "S3"): [
        r"(?:开始|执行)诊断",
        r"(?:检查|查看).*(?:状态|日志|进程)",
    ],
    ("S3", "S4"): [
        r"根据.*(?:结果|证据).*(?:确定|判断)",
        r"诊断(?:完成|结果)：",
    ],
    ("S4", "S5"): [
        r"根因确认：",
        r"(?:建议|推荐)(?:以下)?(?:解决|修复)方案",
    ],
    ("S5", "S6"): [
        r"(?:请|麻烦).*(?:执行|尝试).*(?:后|完成).*(?:反馈|告知)",
        r"执行以上步骤后",
    ],
}

STAGE_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]

class ConversationManager:
    """管理诊断状态转换"""

    def detect_stage_transition(
        self,
        current_stage: str,
        assistant_reply: str,
        user_message: str,
    ) -> str | None:
        """
        分析 GLM 回复，判断是否应进行阶段转换。
        返回新阶段（如 "S1"）或 None（无需转换）
        """
        # 检查重置条件（用户重新描述问题）
        if self._should_reset(user_message):
            return "S0"

        # 检查是否满足当前阶段 → 下一阶段的转换条件
        current_idx = STAGE_ORDER.index(current_stage) if current_stage in STAGE_ORDER else 0
        if current_idx >= len(STAGE_ORDER) - 1:
            return None   # 已在最后阶段

        next_stage = STAGE_ORDER[current_idx + 1]
        transition_key = (current_stage, next_stage)

        if transition_key in STAGE_TRIGGERS:
            for pattern in STAGE_TRIGGERS[transition_key]:
                if re.search(pattern, assistant_reply):
                    return next_stage
        return None

    def _should_reset(self, user_message: str) -> bool:
        """检查用户是否请求重置诊断流程"""
        reset_keywords = ['重新来', '重新开始', '换个问题', '另一个问题', '我要问']
        return any(kw in user_message for kw in reset_keywords)
```

Step 3：在主服务中集成状态机

在 conversation_service.py 的消息处理流程中：
1. 调用 PromptBuilder.build_system_prompt()（传入当前 diagnostic_stage）
2. 获取 GLM 回复后，调用 ConversationManager.detect_stage_transition()
3. 如果有阶段转换，更新 DB 中的 diagnostic_stage

```python
# 伪代码：展示集成点
async def process_message(session_id, user_message):
    session = await db.get_session(session_id)
    
    # 1. 检索知识原子（按当前阶段和用户消息）
    atoms = await kb_client.search_atoms(
        query=user_message,
        category_id=session.category_id,
        stage=session.diagnostic_stage,
    )
    
    # 2. 构建阶段感知 Prompt
    system_prompt = prompt_builder.build_system_prompt(
        diagnostic_stage=session.diagnostic_stage,
        knowledge_atoms=atoms,
        case_context={'case_id': session.case_id, 'description': ...},
        session_state={...},
    )
    
    # 3. 调用 GLM
    reply = await glm_client.chat(messages=[...])
    
    # 4. 检测阶段转换
    new_stage = conversation_manager.detect_stage_transition(
        current_stage=session.diagnostic_stage,
        assistant_reply=reply.content,
        user_message=user_message,
    )
    
    # 5. 持久化
    if new_stage:
        await db.update_session(session_id, diagnostic_stage=new_stage)
    
    return reply
```

Step 4：验证

```bash
# 发送 3 轮消息，验证阶段推进
# 第 1 轮：描述问题
curl -X POST http://localhost:8002/api/v1/conversations/{session_id}/messages \
  -d '{"content": "我的虚拟机 Web-01 开机失败"}'

# 第 2 轮：确认故障
curl -X POST http://localhost:8002/api/v1/conversations/{session_id}/messages \
  -d '{"content": "是的，报错：此主机剩余可配置CPU不足，发生在今天上午10点"}'

# 第 3 轮：查询会话状态，确认 diagnostic_stage 已推进
curl http://localhost:8002/api/v1/conversations/{session_id}
# 预期：diagnostic_stage 从 S0 推进到 S1

# 检查日志，确认 PromptBuilder 注入了对应阶段的 methodology
docker compose logs conversation-service --tail 20 | grep "diagnostic_stage"
```

【约束】
- 阶段转换必须保守（宁可不转换，不要误转换）
- PromptBuilder 每个 segment 方法可独立测试
- 状态机按 DB 优先（不依赖内存缓存）

【验收标准】
- [ ] 经过 3 轮对话后，DB 中 diagnostic_stage 从 S0 推进至 S1
- [ ] PromptBuilder.build_system_prompt() 在 S3 阶段注入的内容与 S0 不同
- [ ] ConversationManager.detect_stage_transition() 有完整单元测试
- [ ] uv run pytest backend/conversation-service/tests/ -v 通过
- [ ] make lint 无新增错误
```

---

# 34_任务编排_P3_ReAct引擎与工具接入

> **阶段**：Phase 3 — ReAct 执行器 + SCP 工具接入（Phase 2 完成后开始）  
> **目标**：实现主动工具调用能力，让 AI Agent 能自主查询 HCI 平台信息（告警/任务/VM状态），而不依赖用户人工描述  
> **并行条件**：T09（GLMClient）可与 T10（ReactExecutor）并行 | T11（SCPAdapter）依赖 T10 完成 | T12（人工确认）依赖 T10完成 | T13（审计日志）依赖 T10/T11 完成  
> **前置依赖**：Task 07（DB 迁移含 tool_audit_log）、Task 08（状态机）  
> **创建日期**：2026-03-22  
> **关联文档**：
> - [docs/architecture/完整技术方案.md](../architecture/完整技术方案.md) § 六、Phase 3
> - [docs/architecture/各层最优设计.md](../architecture/各层最优设计.md) § Layer 2/5
> - [docs/reference/scp/openapi.yaml](../reference/scp/openapi.yaml)（SCP REST API 完整规范）

---