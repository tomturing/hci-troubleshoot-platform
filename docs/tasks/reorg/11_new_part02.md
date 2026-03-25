<!--
  分片标识：新 11_完整技术方案.md（实施规范 HOW）— 第 2/5 部分
  内容来源：原 11 第 103–265 行（Phase 0 Prompt手术 + Phase 1 知识库 + Phase 2 状态机）
  合并目标：新 11 文档第二段
  说明：保持原文，无删减
-->

## 三、Phase 0：Prompt 手术（最快见效）

### 3.1 修改目标文件

[backend/conversation-service/app/services/conversation_service.py](../../backend/conversation-service/app/services/conversation_service.py)

### 3.2 修改内容

**改动1：替换 `_SYSTEM_BASE`（致命约束删除）**

```python
# 删除这段（禁止训练知识）：
_SYSTEM_BASE = """
你是一个专业的HCI（超融合基础设施）智能排障助手...
只能根据下方'参考资料'部分的内容回答问题，不得使用训练数据中的其他知识。
如果参考资料中没有相关内容，必须直接回复：当前知识库暂未收录此类问题...
"""

# 替换为 5段式结构（见 08_HCI平台效果差距分析.md → §五 Prompt 哲学）
```

**改动2：修改空知识库时的处理逻辑**

```python
# 原代码（在 _build_system_prompt 中）：
if not knowledge_chunks:
    system_prompt += "\n\n【重要：当前知识库暂无相关内容】\n禁止推测，只能告知用户知识库无内容。"

# 改为：
if not knowledge_chunks:
    system_prompt += "\n\n【注意：当前知识库暂无该类型问题的历史案例或SOP】\n请基于你的HCI领域机制知识进行推理，明确标注当前分析基于领域知识而非历史案例。"
```

**改动3：加入诊断阶段字段（为 Phase 2 预留）**

```python
# 在 ConversationSession 模型中加入
diagnostic_stage: str = "S0"
```

---

## 四、Phase 1：知识库复活

### 4.1 KB Service 部署检查清单

- [ ] 检查 `backend/kb-service/` 服务代码是否完整
- [ ] 修复 Docker Compose 中 KB Service 的启动配置
- [ ] 验证 pgvector 扩展已安装（`CREATE EXTENSION vector`）
- [ ] 运行向量表迁移（`alembic upgrade head`）
- [ ] 验证 `http://localhost:8004/health` 返回 200

### 4.2 SOP 数据入库流程

**当前 SOP 文件位置**：`data-pipeline/sop_skills/`（600+ MD 文件）

**入库流程**：
```python
# data-pipeline/ingestor.py 核心逻辑
async def ingest_sop(file_path: str):
    # 1. 解析 Markdown frontmatter + 正文
    metadata, content = parse_sop(file_path)

    # 2. 验证 frontmatter（必填字段检查）
    validate_required_fields(metadata, ["category_l1", "category_l2", "keywords"])

    # 3. 切分（按 SOP 章节切分，不用固定 token 数）
    chunks = chunk_by_section(content)

    # 4. 向量化
    embeddings = await embed_batch(chunks)

    # 5. 写入 knowledge_chunks 表
    await db.insert_chunks(chunks, embeddings, metadata)
```

**SOP 格式修复优先级**（按故障频率排序）：
1. 虚拟机 → 无法开机（覆盖率估计最高）
2. 存储 → 磁盘告警处理
3. 虚拟机 → 性能劣化
4. 网络 → 连通性问题
5. 硬件 → 节点告警

---

## 五、Phase 2：诊断状态机

### 5.1 数据库迁移

```sql
-- Phase 2 迁移：conversation_sessions 加诊断状态字段
ALTER TABLE conversation_sessions
    ADD COLUMN diagnostic_stage VARCHAR(8) DEFAULT 'S0',
    ADD COLUMN category_l1 VARCHAR(100),
    ADD COLUMN category_l2 VARCHAR(100),
    ADD COLUMN hypothesis JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN react_state JSONB DEFAULT '{}'::jsonb;

-- Phase 3 迁移：新增工具调用审计表
CREATE TABLE tool_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES conversation_sessions(id),
    trace_id VARCHAR(55),
    tool_name VARCHAR(100) NOT NULL,
    tool_args JSONB NOT NULL,
    risk_level INTEGER NOT NULL,
    policy VARCHAR(20) NOT NULL,
    authorized_by VARCHAR(100),
    result JSONB,
    error TEXT,
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER GENERATED ALWAYS AS (
        EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
    ) STORED
);

CREATE INDEX idx_tool_audit_session ON tool_audit_log(session_id);
CREATE INDEX idx_tool_audit_tool ON tool_audit_log(tool_name, started_at DESC);
```

### 5.2 阶段转换规则

```python
STAGE_TRANSITIONS = {
    "S0": {
        # 判定条件：消息里提到了具体现象描述
        "to_S1": lambda msgs: has_concrete_symptom(msgs),
        "stay": "继续追问症状细节"
    },
    "S1": {
        # 判定条件：LLM 输出了 category_l1 分类
        "to_S2": lambda msgs, session: session.category_l1 is not None,
        "stay": "继续确认故障域"
    },
    "S2": {
        # 判定条件：LLM 输出了 hypothesis 列表（≥2个假设）
        "to_S3": lambda msgs, session: len(session.hypothesis) >= 2,
        "stay": "补充假设"
    },
    "S3": {
        # 判定条件：工具调用已完成，有足够证据
        "to_S4": lambda msgs, session: session.react_state.get("evidence_collected"),
        "stay": "继续收集证据"
    },
    "S4": {
        # 判定条件：LLM 输出了 root_cause（置信度 > 70%）
        "to_S5": lambda msgs, session: session.react_state.get("root_cause_confidence", 0) > 0.7,
        "stay": "进一步验证"
    },
    "S5": {
        # 判定条件：用户确认方案可接受
        "to_S6": lambda msgs: user_accepted_solution(msgs),
        "stay": "修正方案"
    },
}
```
