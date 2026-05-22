---
status: active
category: solution
audience: developer
last_updated: 2026-05-22
version: v1.2
owner: team
---

# SOP 决策树生产与消费流程

> **文档边界说明**
> - **本文档范围**：SOP 决策树的完整生命周期（生产 → 存储 → 消费），涵盖 HTP-agent 与 PAI-agent 的使用差异。
> - **相关文档**：
>   - [SOP多叉决策树设计.md](./SOP多叉决策树设计.md) — 数据模型与解析逻辑
>   - [知识库设计.md](./知识库设计.md) — SOP 整体存储与检索架构

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-05-22 | v1.2 | 简化 API 响应：`GET /api/sop/{id}/tree` 直接返回 SOPNode 树 | — |
| 2026-05-22 | v1.1 | 实现 `approve_sop_document()` 中的 sop_tree 生成逻辑 | — |
| 2026-05-22 | v1.0 | 初版：完整生产/消费流程分析 | — |

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SOP 数据流                                      │
│                                                                             │
│   生产侧                                                     │
│   ┌──────────┐    ┌──────────────┐    ┌──────────────────────────────┐     │
│   │ .docx    │ -> │ import_sop.py│ -> │ POST /api/sop/ingest        │     │
│   │ (Word)   │    │ 解析为 MD    │    │ 写入 sop_document + sop_chunk│     │
│   └──────────┘    └──────────────┘    └──────────────────────────────┘     │
│                                                  │                          │
│                                                  ▼                          │
│                              ┌──────────────────────────────┐               │
│                              │ POST /api/admin/sop/{id}/approve             │
│                              │ 审核通过时自动生成 SOP tree  │               │
│                              └──────────────────────────────┘               │
│                                                  │                          │
│                                                  ▼                          │
│                              ┌──────────────────────────────┐               │
│                              │ sop_tree 表                  │               │
│                              │ tree_json (JSONB)           │               │
│                              └──────────────────────────────┘               │
│                                                                             │
│   消费侧                                                   │
│   ┌──────────────────────────────────────────────────────────────────┐      │
│   │ HTP-agent: KnowledgeRetriever 注入 Markdown 到 System Prompt    │      │
│   │ PAI-agent: get_sop_tree() Tool 按需获取结构化树                  │      │
│   └──────────────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、生产流程详解

### 2.1 阶段一：文档导入

**入口**：`data-pipeline/kbd/import_sop.py`

**CLI 命令**：

```bash
# 单文件导入
python -m kbd.import_sop --file /path/to/sop.docx --category-id "虚拟机-001"

# 批量导入
python -m kbd.import_sop --dir /path/to/sop_docs/ --category-id "虚拟机-001"
```

**处理流程**：

```
.docx 文件
    │
    ▼ python-docx 解析
┌─────────────────────────────────────────────────┐
│ parse_docx_to_markdown()                        │
│                                                 │
│ 输入：Word 文档（Heading 1/2/3 标题结构）        │
│ 输出：                                          │
│   - title: 文档标题                             │
│   - content_md: 完整 Markdown 文本              │
│   - chapters: [(章节标题, 内容), ...]           │
└─────────────────────────────────────────────────┘
    │
    ▼ SHA256 哈希
┌─────────────────────────────────────────────────┐
│ compute_docx_hash()                             │
│                                                 │
│ 生成 docx_hash 用于幂等去重                      │
└─────────────────────────────────────────────────┘
    │
    ▼ HTTP POST
┌─────────────────────────────────────────────────┐
│ POST /api/sop/ingest                            │
│                                                 │
│ 请求体：                                        │
│ {                                               │
│   "source_id": "sop-vm-start-failure",         │
│   "title": "虚拟机开机失败排查SOP",             │
│   "content_md": "# 场景概述\n## ...",          │
│   "category_id": "虚拟机-003",                  │
│   "docx_hash": "a1b2c3..."                      │
│ }                                               │
└─────────────────────────────────────────────────┘
    │
    ▼ kb-service 处理
┌─────────────────────────────────────────────────┐
│ POST /api/sop/ingest (sop_ingest.py)           │
│                                                 │
│ 1. 幂等检查（docx_hash / source_id）            │
│ 2. 创建 sop_document 记录（status=draft）       │
│ 3. split_by_chapters() 按章节分块               │
│ 4. 批量写入 sop_chunk 表                        │
└─────────────────────────────────────────────────┘
```

**数据表结果**：

| 表 | 字段 | 说明 |
|---|---|---|
| `sop_document` | id, title, content_md, category_id, status | 文档元数据 + 完整 Markdown |
| `sop_chunk` | document_id, chunk_index, chapter_title, content | 按章节分块（供 RAG 检索） |

---

### 2.2 阶段二：决策树解析

**入口**：`backend/kb-service/app/services/sop_parser.py`

**核心函数**：

```python
from app.services.sop_parser import parse_sop_markdown

result = parse_sop_markdown(content_md)
# result.tree → SOPNode 根节点
# result.errors → 阻断性问题（叶节点缺 diagnosis/solution）
# result.warnings → 非阻断性问题（话术不规范等）
```

**解析流程**：

```
Markdown 文本
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ _parse_into_sections()                                      │
│                                                             │
│ 扫描 Markdown 标题（# ~ ######），识别：                      │
│   - node 标题：普通标题（场景/类别/案例名称）                 │
│   - diagnosis 标题：含"判断方法/排查方法"等关键词            │
│   - solution 标题：含"解决方案/处理方法"等关键词             │
│                                                             │
│ 输出：[_SectionEntry(level, text, section_type, content)]   │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ _build_tree()                                               │
│                                                             │
│ 使用栈追踪层级，构建多叉树：                                  │
│   - node 段落 → 创建 SOPNode，按层级挂载到父节点             │
│   - diagnosis 段落 → 填充到最近祖先节点的 diagnosis 字段      │
│   - solution 段落 → 填充到最近祖先节点的 solution 字段        │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ _validate_leaves()                                          │
│                                                             │
│ 叶节点完整性校验：                                           │
│   - children=[] 且缺少 diagnosis → error（阻断入库）         │
│   - children=[] 且缺少 solution → error                     │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ _assign_node_ids()                                          │
│                                                             │
│ 分配 node_id：n-1, n-1-1, n-1-2, n-1-2-1...                │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
SOPNode 树（存入 sop_tree.tree_json）
```

**关键词识别规则**：

| 类型 | 等效关键词 |
|------|-----------|
| diagnosis | 判断方法、判断依据、排查方法、排查步骤、识别方法、确认方法、诊断方法 |
| solution | 解决方案、解决方法、处理方法、处理步骤、修复方法、修复步骤、解决步骤 |

**SOPNode 数据结构**：

```python
class SOPNode:
    node_id: str          # "n-1-2-3" 格式
    name: str             # 节点名称（来自标题）
    level: int            # 标题层级（H1=1, H2=2...）
    prerequisites: list   # 前置检查条件
    diagnosis: DiagnosisDetail | None   # 判断方法（叶节点必填）
    solution: SolutionDetail | None     # 解决方案（叶节点必填）
    children: list[SOPNode]             # 子节点

class DiagnosisDetail:
    prerequisites: list   # 前置检查
    page_methods: list    # 页面判断方法（必填）
    acli_methods: list    # acli 命令行方法
    description: str      # 判断说明
    root_cause: str       # 问题根因
    notes: str            # 注意事项

class SolutionDetail:
    quick_recovery: list  # 快速恢复方案
    thorough_fix: list    # 彻底解决方案
```

---

### 2.3 阶段三：审核发布与 Tree 生成

**入口**：`POST /api/admin/sop/{document_id}/approve`

**流程**：

```
POST /api/admin/sop/{document_id}/approve
    │
    ▼
┌─────────────────────────────────────────────────┐
│ approve_sop_document()                          │
│                                                 │
│ 1. 查询 sop_document + sop_chunk               │
│ 2. 调用 parse_sop_markdown() 生成决策树         │
│ 3. 写入 sop_tree 表（tree_json）                │
│ 4. 遍历 sop_chunk 生成 embedding（向量化）       │
│ 5. 生成 BM25 索引（to_tsvector）                │
│ 6. 更新 status → published                     │
│ 7. 设置 published_at                            │
└─────────────────────────────────────────────────┘
```

---

## 三、消费流程详解

### 3.1 HTP-agent 消费方式

**特点**：知识前置注入，调用 LLM 前已将 SOP Markdown 注入 System Prompt。

**流程**：

```
用户消息
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ KnowledgeRetriever.retrieve()                               │
│                                                             │
│ S0 阶段：禁止 KB/SOP 检索（避免过早锁定）                    │
│ S1+ 阶段：                                                   │
│   1. classify_intent(query) → category_id                  │
│   2. route_by_category(category_id) → SOP/KBD/降级         │
│   3. 若 SOP 命中：注入 SEGMENT_SOP_REFERENCE                │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 5段式 System Prompt                                         │
│                                                             │
│ Segment 1: 专家身份定义                                     │
│ Segment 2: 诊断方法论（随阶段变化）                          │
│ Segment 3: HCI 机制知识                                     │
│ Segment 4: SOP Markdown 文本 ← 从 sop_document.content_md  │
│ Segment 5: 工单上下文                                       │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
LLM 推理 → 文本流输出
```

**Segment 4 SOP 注入格式**：

```
【SOP 排障流程 | 来源：虚拟机开机失败排查SOP】

# 场景概述
...

## Redis OOM 导致开机失败

### 判断方法
- 页面判断方法：...
- acli 命令行：...

### 解决方案
- 快速恢复方案：...
- 彻底解决方案：...

请严格按照上述排障流程执行，在每个判断节点收集证据后再做决策。
```

---

### 3.2 PAI-agent 消费方式

**特点**：知识按需获取，pydantic-ai Agent 通过 Tool 动态调用。

**Tool 定义**：

```python
@agent.tool
async def get_sop_tree(ctx: RunContext[PydanticAIDeps], document_id: int) -> dict:
    """获取 SOP 标准操作流程决策树，用于按步骤引导故障处理。

    树中每个节点包含 prerequisites（前置条件）、diagnosis（判断方法）、
    solution（解决方案）、children（子节点）。
    只有当你已知 SOP 文档 ID 时才调用此工具。

    Args:
        document_id: SOP 文档 ID（从意图识别结果或历史对话中获取）
    """
    if ctx.deps.kb_client is None:
        return {"error": "KB 服务不可用，无法获取 SOP 决策树"}
    result = await ctx.deps.kb_client.get_sop_tree(document_id)
    if result is None:
        return {"error": f"SOP 文档 {document_id} 的决策树不存在或尚未生成"}
    return result
```

**消费流程**：

```
用户提问："虚拟机开机失败怎么排查？"
    │
    ▼ pydantic-ai Agent 推理
┌─────────────────────────────────────────────────────────────┐
│ Agent 决定调用 get_sop_tree(document_id=123)                │
│                                                             │
│ 1. LLM 分析用户问题，识别可能匹配的 SOP 文档 ID              │
│    (document_id 可能来自：意图识别结果 / 知识库匹配)          │
│                                                             │
│ 2. Tool 执行：                                              │
│    KBClient.get_sop_tree(123)                               │
│      → GET http://kb-service:8004/api/sop/123/tree          │
│                                                             │
│ 3. 返回 tree_json（SOPNode.model_dump() 格式）              │
└─────────────────────────────────────────────────────────────┘
    │
    ▼ Agent 遍历决策树
┌─────────────────────────────────────────────────────────────┐
│ Agent 解析树结构，按步骤引导用户：                            │
│                                                             │
│ 1. 从根节点出发                                              │
│ 2. 检查 prerequisites 条件                                  │
│ 3. 进入满足条件的子节点                                      │
│ 4. 到达叶节点后：                                            │
│    - 执行 diagnosis 判断（确认问题匹配）                      │
│    - 输出 solution（快速恢复 + 彻底解决）                    │
└─────────────────────────────────────────────────────────────┘
    │
    ▼ 流式输出
AgentTextChunk → 前端 SSE
```

---

## 四、HTP-agent vs PAI-agent SOP 使用对比

| 维度 | HTP-agent | PAI-agent |
|------|-----------|-----------|
| **SOP 获取时机** | 调用前预注入 System Prompt | 运行时 Tool 按需调用 |
| **获取方式** | `KnowledgeRetriever.route_by_category()` | `get_sop_tree(document_id)` |
| **数据格式** | Markdown 文本（注入 Prompt） | 结构化 SOPNode 树 |
| **执行方式** | LLM 自行解读文本 | Agent 程序遍历树节点 |
| **交互模式** | 单轮文本生成 | 多轮 Tool 调用 + 验证 |
| **适用场景** | 标准故障（已有 SOP 匹配） | 未知故障探索、需要实时数据 |
| **Token 消耗** | 较高（全文注入） | 较低（按需获取） |
| **可控性** | 低（LLM 自由解读） | 高（程序化遍历） |

---

## 五、数据库表关系

```
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│ sop_document     │ 1:1   │ sop_tree         │ 1:N   │ sop_chunk        │
├──────────────────┤───────├──────────────────┤───────├──────────────────┤
│ id               │       │ id               │       │ id               │
│ title            │       │ document_id (FK) │       │ document_id (FK) │
│ content_md       │       │ scenario_name    │       │ chunk_index      │
│ category_id      │       │ tree_json (JSONB)│       │ chapter_title    │
│ status           │       │ leaf_count       │       │ content          │
│ docx_hash        │       │ total_node_count │       │ embedding        │
│ published_at     │       │ validation_status│       │ tsv              │
└──────────────────┘       └──────────────────┘       └──────────────────┘

         │
         │ 1:N
         ▼
┌──────────────────┐
│ sop_chunk        │
│ (供 RAG 检索)     │
└──────────────────┘
```

---

## 六、API 端点汇总

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/sop/ingest` | POST | 导入 SOP 文档（创建 sop_document + sop_chunk） |
| `/api/sop/{document_id}/tree` | GET | 获取 SOP 决策树（返回 tree_json） |
| `/api/admin/sop/{document_id}/approve` | POST | 审核通过（生成 embedding + tree + 发布） |
| `/api/kb/route` | GET | 三轨路由（HTP-agent 使用） |

---

## 七、示例：SOP 文档到决策树

**输入 Markdown**：

```markdown
# 虚拟机开机失败

## Redis OOM 导致开机失败

### 判断方法
- 页面判断方法：
  - 查看 Redis 内存使用率 > 90%
  - 查看 /var/log/redis/redis.log 有 OOM 记录
- acli 命令行：
  - acli redis info memory

### 解决方案
- 快速恢复方案：
  - 重启 Redis 服务
  - 释放大 key
- 彻底解决方案：
  - 扩容 Redis 内存
  - 配置内存淘汰策略

## 存储不可访问导致开机失败

### 判断方法
- 页面判断方法：
  - 查看存储池状态为"异常"
  - 查看 ASAN 日志有 I/O 错误

### 解决方案
- 快速恢复方案：
  - 重启存储服务
- 彻底解决方案：
  - 检查存储网络连接
  - 更换故障磁盘
```

**输出 tree_json**：

```json
{
  "node_id": "n-1",
  "name": "虚拟机开机失败",
  "level": 1,
  "prerequisites": [],
  "children": [
    {
      "node_id": "n-1-1",
      "name": "Redis OOM 导致开机失败",
      "level": 2,
      "prerequisites": [],
      "diagnosis": {
        "page_methods": [
          "查看 Redis 内存使用率 > 90%",
          "查看 /var/log/redis/redis.log 有 OOM 记录"
        ],
        "acli_methods": ["acli redis info memory"]
      },
      "solution": {
        "quick_recovery": ["重启 Redis 服务", "释放大 key"],
        "thorough_fix": ["扩容 Redis 内存", "配置内存淘汰策略"]
      },
      "children": []
    },
    {
      "node_id": "n-1-2",
      "name": "存储不可访问导致开机失败",
      "level": 2,
      "prerequisites": [],
      "diagnosis": {
        "page_methods": [
          "查看存储池状态为\"异常\"",
          "查看 ASAN 日志有 I/O 错误"
        ],
        "acli_methods": []
      },
      "solution": {
        "quick_recovery": ["重启存储服务"],
        "thorough_fix": ["检查存储网络连接", "更换故障磁盘"]
      },
      "children": []
    }
  ]
}
```

---

## 八、错误处理与降级

| 场景 | 处理方式 |
|------|---------|
| SOP tree 不存在（未审核） | 返回 404，PAI-agent 降级到其他 Tool |
| 解析错误（叶节点缺字段） | 阻断入库，记录 error |
| 话术不规范（非标准关键词） | 允许入库，记录 warning |
| KB 服务不可用 | Tool 返回 error 信息，Agent 降级处理 |

---

## 九、运维命令

```bash
# 导入 SOP 文档
python -m kbd.import_sop --file /path/to/sop.docx --category-id "虚拟机-001"

# 审核发布（生成 tree + embedding）
curl -X POST http://localhost:8004/api/admin/sop/1/approve \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": "admin"}'

# 查询 SOP tree
curl http://localhost:8004/api/sop/1/tree \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN"
```
