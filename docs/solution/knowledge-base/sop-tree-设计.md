---
status: active
category: solution
audience: developer
last_updated: 2026-05-01
version: v1.0
owner: team
---

# SOP 多叉决策树设计

> **文档边界说明**
> - **本文档范围**：SOP 结构化决策树的数据模型、解析逻辑、存储方案、双向同步规则。
> - **不在本文档**：AI Agent 的行为策略（S1~S6 阶段）、SOP 向量检索（sop_chunk）、前端 UI 交互。
> - **相关文档**：[知识库设计.md](./知识库设计.md) — SOP 整体存储与检索架构。

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-05-19 | v1.1 | 解析策略重构：叶优先/关键词匹配；`sop_template.py` 移除 model_validator（宽松模式）；DiagnosisDetail/SolutionDetail 新增 `source_heading` 溯源字段；§2.2 去掉 H5 固定假设；§3 模型说明更新；§5.1 解析流程更新；新增 §八 validation_issues 三层作用 | [2026-05-19-SOP解析策略与校验模式设计.md](./events/2026-05-19-SOP解析策略与校验模式设计.md) |
| 2026-05-01 | v1.0 | 初版：多叉决策树设计，sop_tree 表，Pydantic 模型，双向同步规则 | — |

---

## 一、背景与问题

### 1.1 现状

SOP 文档目前以纯 Markdown 形式存储在 `sop_document.content_md`，AI Agent 通过 RAG
检索 `sop_chunk` 获取相关段落，再由 LLM 推理执行步骤。

这个方案存在以下问题：

| 问题 | 影响 |
|------|------|
| SOP 是树形逻辑，但 RAG 只返回"片段" | Agent 拿到的是局部段落，不知道当前处于整棵决策树的哪个分支 |
| 前置检查（prerequisites）嵌入 Markdown 正文 | Agent 无法程序化读取"进入某分支前需满足哪些条件" |
| 无结构化输出 | 无法统计叶节点数、无法断言 Agent 的遍历覆盖率 |

### 1.2 目标

为每个 `sop_document` 生成一个 **对应的结构化多叉决策树**（存在 `sop_tree` 表），使得：

1. AI Agent（ReAct）可以程序化遍历决策树，按"点-边"逻辑执行排障
2. 工程师可以直接操作 JSON 修复结构，再反向同步回 Markdown
3. 支持多格式导入导出（docx → markdown → JSON，JSON → markdown）

---

## 二、核心概念：多叉决策树

### 2.1 点-边 决策树范式

```
      [ 服务组件异常 ]  ← 根节点（根是 SOPNode，不是特殊类型）
           │
     ┌─────┴─────┐
   [Redis OOM] [MySQL 慢查询]   ← 中间节点（路由，有子节点）
       │
  ┌────┴────┐
[内存限制不足] [Key 未设 TTL]   ← 叶节点（案例，无子节点）
```

- **点（节点）**：代表"当前已定位到的故障场景/类别/具体案例"
- **边（检查）**：进入某个节点需要满足的**前置检查条件**（存在 `prerequisites` 字段）
- **叶节点**：无子节点，代表具体故障案例，必须有 `diagnosis`（判断方法）和 `solution`（解决方案）
- **中间节点**：有子节点，代表故障分类，只需 `name + prerequisites`，无需 `diagnosis/solution`

### 2.2 与文档 Heading 的对应关系

| docx Heading | 节点类型 | 说明 |
|-------------|---------|------|
| H1（场景名称） | 根节点 | 整棵树的入口，`level=1` |
| H2（大类名称） | 中间节点 | 第一层路由，可选 |
| H3（类别名称） | 中间节点 | 第二层路由，可选 |
| H4+（详细案例）| 叶节点候选 | 具体案例，深度可变；**是否为叶节点由关键词匹配决定，不由层级决定** |
| 任意层级（含「判断方法」类关键词）| 叶节点内部结构 | **不是独立树节点**，解析为 `diagnosis` 字段；`source_heading` 记录原始文本 |
| 任意层级（含「解决方案」类关键词）| 叶节点内部结构 | **不是独立树节点**，解析为 `solution` 字段；`source_heading` 记录原始文本 |

> 树深度不固定：可以 H1→H2（只有两层），可以 H1→H2→H4（跳过 H3），也可以 H1→H2→H3→H4→H5→H6。
> diagnosis/solution 段落识别依据是**关键词语义匹配**，不是固定的 H5。

### 2.3 为什么没有独立的 SOPDecisionTree 根类型？

**统一节点原则**：所有节点（根节点、中间节点、叶节点）都使用相同的 `SOPNode` 类型。

原因：
- 根节点与其他节点的唯一区别是"它没有父节点"，这是**位置信息**，不需要独立类型表达
- `schema_version`、`generated_at` 等元数据属于**存储层**，存在 `sop_tree` 表的列中，不进 JSON payload
- `scenario_name` 就是 `root_node.name`，无需重复
- 统一类型使遍历算法不需要特判根节点

---

## 三、Pydantic 数据模型

位置：`backend/kb-service/app/schemas/sop_template.py`

### 3.1 三个核心类

```python
class DiagnosisDetail(BaseModel):
    """叶节点内部：判断方法段落（任意层级，关键词匹配）"""
    prerequisites: list[str] = []        # 判断前的前置检查（可选）
    page_methods: list[str]              # 页面判断方法（必填，至少 1 项）
    acli_methods: list[str] = []         # acli 判断方法（可选）
    description: str | None = None       # 判断说明（可选）
    root_cause: str | None = None        # 问题根因（可选）
    notes: str | None = None             # 注意事项（可选）
    source_heading: str | None = None    # 溯源：docx 原始标题文本（如"排查方法"），仅审计用


class SolutionDetail(BaseModel):
    """叶节点内部：解决方案段落（任意层级，关键词匹配）"""
    quick_recovery: list[str]            # 快速恢复方案（必填，至少 1 项）
    thorough_fix: list[str]              # 彻底解决方案（必填，至少 1 项）
    source_heading: str | None = None    # 溯源：docx 原始标题文本（如"处理方法"），仅审计用


class SOPNode(BaseModel):
    """统一决策树节点（中间节点和叶节点共用，宽松模式：残缺节点可构建）"""
    node_id: str = ""                    # 自动生成，格式 n-1-2-3（路径编码）
    name: str                            # 节点名称（来自 Heading 文本）
    level: int = 1                       # 来自 Heading 级别，仅元数据
    prerequisites: list[str] = []       # 边：进入此节点的前置检查条件
    diagnosis: DiagnosisDetail | None = None   # 叶节点期望有，缺失由校验层记 error
    solution: SolutionDetail | None = None     # 叶节点期望有，缺失由校验层记 error
    children: list[SOPNode] = []         # 子节点列表（空 = 叶节点）
```

### 3.2 校验规则（宽松模式）

> **重要**：Pydantic 模型层不执行叶节点完整性校验（无 `model_validator`）。
> 残缺节点可以正常构建。完整性检查由解析器层（`sop_parser.py`）执行，
> 结果写入 `SOPValidationResult`。

```
校验层（sop_parser.py）检查项：

叶节点（children == []）：
  → diagnosis 为 None         → error（阻断入库）
  → solution 为 None          → error（阻断入库）
  → source_heading 非标准词   → warning（允许入库，展示差异报告）

中间节点（children 非空）：
  → diagnosis/solution 非 None → warning（意外内容，提示人工确认）

任意节点：
  → 未识别段落（关键词不匹配） → warning（提示人工确认）
```

### 3.3 前置检查（prerequisites）

所有层级的 `prerequisites` 类型统一为 `list[str]`，不区分 filter/sequence 类型。

AI Agent 在遍历时自行决定如何使用这些条件（通常：当前情境满足所有 prerequisites 时才进入该节点）。

---

## 四、数据库表 sop_tree

### 4.1 表结构

```sql
CREATE TABLE sop_tree (
    id                  SERIAL          NOT NULL,
    document_id         INTEGER         NOT NULL,       -- 1:1 关联 sop_document.id
    schema_version      VARCHAR(20)     NOT NULL DEFAULT 'sop-tree-v1',
    scenario_name       VARCHAR(500)    NOT NULL,       -- 冗余根节点 name
    tree_json           JSONB           NOT NULL,       -- SOPNode 根节点 model_dump()
    leaf_count          INTEGER         NOT NULL DEFAULT 0,
    total_node_count    INTEGER         NOT NULL DEFAULT 0,
    validation_status   VARCHAR(20)     NOT NULL DEFAULT 'valid',  -- valid/warnings/error
    validation_issues   JSONB                    DEFAULT NULL,
    generated_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    generator_version   VARCHAR(50)              DEFAULT 'sop-parser-v1',
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT sop_tree_pkey          PRIMARY KEY (id),
    CONSTRAINT sop_tree_document_uniq UNIQUE (document_id),
    CONSTRAINT fk_sop_tree_document   FOREIGN KEY (document_id)
        REFERENCES sop_document (id) ON DELETE CASCADE
);
```

### 4.2 与 sop_document 的关系

```
sop_document (1) ──── (1) sop_tree
     │                      │
     │ content_md (Markdown) │ tree_json (JSON 决策树)
     │                      │
     └──── sop_chunk[]       │
           (RAG 检索用)       └── 供 AI Agent 遍历
```

- `sop_document` 删除时，`sop_tree` 级联删除
- `sop_tree.updated_at` 在任何同步操作后更新

### 4.3 validation_status 状态说明

| 状态 | 含义 | 入库 |
|------|------|------|
| `valid` | 无任何校验问题 | ✓ |
| `warnings` | 有非阻断警告（如可选字段缺失） | ✓（警告展示给用户） |
| `error` | 有阻断性错误（如叶节点缺少判断方法） | ✗（上传 API 返回 422） |

---

## 五、解析流程：docx → JSON 决策树

```
┌──────────────────┐     ┌───────────────────┐     ┌──────────────────────┐
│  上传 .docx 文件  │────▶│  _parse_docx_bytes │────▶│  parse_sop_markdown  │
│  (admin.py)      │     │  (Markdown 转换)   │     │  (sop_parser.py)     │
└──────────────────┘     └───────────────────┘     └──────────┬───────────┘
                                                               │
                                              ┌────────────────▼─────────────────┐
                                              │  SOPValidationResult              │
                                              │  ├─ is_valid: bool                │
                                              │  ├─ errors: [ValidationIssue...]  │
                                              │  ├─ warnings: [ValidationIssue...]│
                                              │  └─ tree: SOPNode | None          │
                                              └────────────────┬─────────────────┘
                                                               │
                            ┌──────────────────────────────────┤
                            │ errors → 返回 422（不入库）       │ success → 入库
                            ▼                                  ▼
                      ┌─────────────┐               ┌──────────────────┐
                      │  422 响应    │               │  写 sop_document  │
                      │  (上传失败) │               │  写 sop_tree      │
                      └─────────────┘               └──────────────────┘
```

### 5.1 解析器状态机（sop_parser.py — 待实现）

解析 Markdown → SOPNode 树的状态机逻辑（**叶优先策略**）：

1. **扫描 Heading**：识别任意层级 `#`（不截断，不限制层级上限）
2. **标题分类**：对每个 Heading 文本执行关键词匹配
   - 含「判断方法」类关键词 → `diagnosis` 段落，记录 `source_heading=原文`
   - 含「解决方案」类关键词 → `solution` 段落，记录 `source_heading=原文`
   - 其他 → 普通节点标题
3. **构建树**：
   - 最顶层 Heading → 根节点
   - 普通节点标题 → 按**相对层级差**挂到父节点（不依赖绝对 H 数字）
   - `diagnosis`/`solution` 段落 → 挂到当前节点候选的内部字段，**不作为子节点**
4. **识别列表项**：支持 `- `、`* `、`1. `、`1、`（中文顿号编号）
5. **延迟叶节点判断**：扫描完成后，children=[] 的节点才判断是否完整
   - 有 diagnosis+solution → 合法叶节点
   - 缺任一 → 生成 `ValidationIssue(level="error", ...)`（不阻断构建，阻断入库）

**关键词等效表**（解析器唯一来源，不在模型层重复）：

| 字段 | 等效关键词（任一匹配即可）|
|------|------------------------|
| `diagnosis` | 判断方法、判断依据、排查方法、排查步骤、识别方法、确认方法、诊断方法 |
| `solution`  | 解决方案、解决方法、处理方法、处理步骤、修复方法、修复步骤、解决步骤 |

### 5.2 node_id 生成规则

格式：`n-{根序号}-{子序号}-{孙序号}...`，例：
- 根节点：`n-1`
- 第二个 H2 子节点：`n-1-2`
- 第二个 H2 的第三个 H3 子节点：`n-1-2-3`

node_id 是树路径的编码，在双向同步时保持稳定（只要文档结构不变）。

---

## 六、双向同步

### 6.1 Markdown → JSON（正向，自动触发）

触发时机：
- docx 上传入库时
- 手动重新解析 API 调用时

流程：
```python
content_md = sop_document.content_md
result = parse_sop_markdown(content_md)   # 返回 SOPValidationResult
if result.is_valid or result.errors == []:
    sop_tree.tree_json = result.tree.model_dump()
    sop_tree.updated_at = now()
```

### 6.2 JSON → Markdown（反向，手动触发）

触发时机：
- 工程师直接编辑 `sop_tree.tree_json` 后调用 `POST /api/admin/sop/{id}/sync-tree-to-markdown`

流程：
```python
root = SOPNode.model_validate(sop_tree.tree_json)
content_md = render_sop_markdown(root)   # sop_tree.py 中的 sop_to_markdown()
sop_document.content_md = content_md
sop_document.updated_at = now()
```

### 6.3 一致性约束

- `sop_document.content_md` 和 `sop_tree.tree_json` 理论上是**同一信息的两种表示**
- 若解析失败（`validation_status=error`），`sop_tree` 不存在，仅 `sop_document` 存在
- 任何同步操作必须在单个数据库事务中完成（避免部分更新）

---

## 七、API 接口变更

### 7.1 上传接口 `POST /api/admin/sop/upload` 响应变更

新增字段：
```json
{
  "id": 42,
  "chunks_created": 8,
  "duplicate": false,
  "validation": {
    "is_valid": true,
    "error_count": 0,
    "warning_count": 2,
    "warnings": [
      {"level": "warning", "location": "Redis OOM > 案例1", "message": "判断说明（可选）未填写"}
    ]
  },
  "decision_tree_summary": {
    "scenario_name": "服务组件异常",
    "leaf_count": 12,
    "total_node_count": 20,
    "validation_status": "warnings"
  }
}
```

有 `errors`（`validation.is_valid=false`）时返回 HTTP 422，不写库。

### 7.2 新增接口

| Method | Path | 说明 |
|--------|------|------|
| `GET` | `/api/admin/sop/{id}/tree` | 获取决策树 JSON |
| `PUT` | `/api/admin/sop/{id}/tree` | 直接更新决策树 JSON |
| `POST` | `/api/admin/sop/{id}/sync-markdown-to-tree` | 重新从 Markdown 解析决策树 |
| `POST` | `/api/admin/sop/{id}/sync-tree-to-markdown` | 从决策树 JSON 重新渲染 Markdown |

---

## 八、validation_issues 三层作用

`sop_tree.validation_issues`（JSONB）不仅是错误展示字段，有三层价值：

### Layer 1：即时反馈（上传时）

上传接口响应直接返回 `SOPValidationResult`，告知用户具体错误位置：

```json
{
  "validation": {
    "is_valid": false,
    "errors": [
      {"level": "error", "location": "服务异常 > Redis OOM > 案例1", "message": "缺少解决方案段落"}
    ]
  }
}
```

生命周期：HTTP 响应结束即丢弃。

### Layer 2：持久化审计

`sop_tree.validation_issues` 存入数据库，事后查询：

```sql
-- 查找所有带 warning 的文档（话术不规范）
SELECT document_id, validation_issues
FROM sop_tree
WHERE validation_status = 'warnings'
  AND validation_issues @> '[{"level": "warning"}]';
```

### Layer 3：异类检测原材料

跨文档聚合 `source_heading`，找出话术不规范文档：

```sql
-- 统计 source_heading 分布，低频 = 异类
SELECT src->>'source_heading' AS heading_text, COUNT(*) AS doc_count
FROM sop_tree, jsonb_array_elements(validation_issues) AS src
WHERE src->>'message' LIKE '%话术%'
GROUP BY heading_text
ORDER BY doc_count ASC;
```

例：发现 45 份文档用"解决方案"，5 份用"处理方法"——后者为异类，可触发批量话术归一流程。

---

## 九、注意事项 / 已知约束

1. **现有 sop_document 数据**：历史数据没有对应 `sop_tree`，需要重新上传 docx 或触发 `sync-markdown-to-tree` 批量补建
2. **`_parse_docx_bytes()` 修复**：`admin.py` 中 `heading_prefix = "#" * min(level, 3)` 需改为 `"#" * level`，否则 H4+ 标题被压缩（Task T2）
3. **中文编号列表**：`1、2、` 格式（中文顿号）已支持；`（1）（2）` 括号格式暂不支持
4. **宽松模式的数据一致性**：`validation_status=error` 时 `tree_json` 仍可能存在（残缺树），消费方需检查 `validation_status` 再使用
5. **pydantic-ai 集成**：本文档设计的 `SOPNode` 结构将作为 pydantic-ai Agent（C 大脑）的 `@agent.tool` 返回类型，在 A/B/C 三向测试方案中直接使用。  
   - `SOPNode` 是 `pydantic.BaseModel`，pydantic-ai 工具系统天然支持 Pydantic 类型作为工具返回类型，无需手写 JSON Schema  
   - LLM 可以直接以结构化 JSON 形式访问 `prerequisites`、`diagnosis.page_methods`、`solution.quick_recovery` 等字段  
   - 集成方式：`@agent.tool async def get_sop_node(...) -> SOPNode`，pydantic-ai 自动序列化并传递给 LLM  
   - 验证时间：2026-05-19（已确认 GLMClient 使用 `AsyncOpenAI(base_url=...)` 接入 GLM，OpenAI-compatible，pydantic-ai 零适配成本接入）  
   - 相关文档：[大脑可选-集成重设计方案.md §十一](../agent/大脑可选-集成重设计方案.md)
