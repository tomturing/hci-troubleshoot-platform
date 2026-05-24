---
status: active
category: solution
audience: developer
last_updated: 2026-05-24
owner: kb-service
---

# RAG（检索增强生成）技术原理

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道项目 KBD 案例 |
| **幻觉** | 模型在"不知道"时仍然生成 | 编造不存在的解决方案 |

### RAG 的第一性原理解法

将问题拆解为两个子问题：

```
P(answer | question)
= P(answer | question, context) × P(context | question)
           ↑                              ↑
    LLM 的擅长：给定上下文推理      检索系统的擅长：找相关文档
```

**核心洞察**：LLM 在有正确上下文时，推理能力极强且幻觉大幅减少。问题不是让 LLM "记住"所有知识，而是在推理时动态注入相关知识。

---

## 二、RAG 的完整架构

```
╔══════════════════════════════════════════════════════════╗
║                    INDEXING PIPELINE（离线）              ║
║                                                          ║
║  原始文档 → [分块] → [嵌入] → [向量数据库]               ║
╚══════════════════════════════════════════════════════════╝
                          ↕
╔══════════════════════════════════════════════════════════╗
║                   QUERY PIPELINE（在线）                  ║
║                                                          ║
║  用户问题 → [查询嵌入] → [检索] → [重排序] → [生成]      ║
╚══════════════════════════════════════════════════════════╝
```

---

## 三、核心组件：分块（Chunking）

### 为什么要分块

向量 embedding 是**定长向量**（如 1536 维）。将整篇文档压入一个向量，细节信息会被"平均化"而丢失。分块让每个向量表达一个**语义完整的局部概念**。

### 分块策略的演进

**Level 1：固定大小分块（最简单，最差）**
```python
chunks = [text[i:i+512] for i in range(0, len(text), 512)]
```
问题：可能将一句话切断，破坏语义完整性。

**Level 2：句子/段落边界分块**
```python
# 按自然语言边界切分
chunks = split_by_paragraph(text, max_tokens=512, overlap=50)
```
`overlap` 是关键——相邻 chunk 共享 50 个 token，防止答案跨边界丢失。

**Level 3：语义分块（Semantic Chunking）**

计算相邻句子的 embedding 余弦相似度，在**语义断裂点**切割：

```python
# 相似度骤降 → 语义边界 → 切割点
similarities = [cosine(embed(s[i]), embed(s[i+1])) for i in range(n)]
breakpoints = [i for i where similarities[i] < threshold]
```

**Level 4：层级分块（Hierarchical / Parent-Child）**

```
文档
├── 大块（512 tokens）← 用于上下文注入（context window）
│   ├── 小块（128 tokens）← 用于精确检索（索引）
│   ├── 小块
│   └── 小块
└── 大块
    ├── 小块
    └── 小块
```

**检索**用小块（精度高），**注入 LLM**用父大块（上下文完整）。这是目前业界准确率最高的方案之一。

---

## 四、核心组件：嵌入（Embedding）

### Embedding 的本质

将文本映射到高维向量空间，使**语义相似的文本距离相近**：

$$\text{embed}(\text{"虚拟机无法启动"}) \approx \text{embed}(\text{"VM fails to start"})$$

度量相似度最常用余弦距离：

$$\text{similarity} = \frac{\vec{q} \cdot \vec{d}}{|\vec{q}||\vec{d}|}$$

### 对称 vs 非对称检索

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，不同字段对向量的贡献是加权叠加的。

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似——这会导致错误召回。

**结论：Embedding 应只包含问题侧字段**（`title + problem_description + alert_info + root_cause`），不包含 `solution`、`recommendations` 等答案侧字段。

---

## 五、核心组件：检索（Retrieval）

### 三种检索范式

**向量检索（Semantic Search）**

```sql
SELECT * FROM kbd_entry
ORDER BY embedding <=> query_vector  -- pgvector cosine distance
LIMIT 10
```

- ✓ 语义泛化（同义词、跨语言）
- ✗ 精确关键字匹配弱（"0x0CFFFFFF" 这种错误码）

**关键字检索（BM25 / Full-Text）**

```sql
SELECT *, ts_rank(tsv, query) AS rank
FROM kbd_entry, to_tsquery('simple', '0x0CFFFFFF') query
WHERE tsv @@ query
ORDER BY rank DESC
```

BM25 的打分公式：

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b + b \cdot \frac{|D|}{\text{avgdl}})}$$

- ✓ 精确关键字（错误码、产品名）
- ✗ 语义泛化弱

**混合检索（Hybrid Search）**

```python
# RRF（Reciprocal Rank Fusion，更稳健，无需调参）
score_rrf(d) = Σ 1 / (k + rank_i(d))    # k=60 经验值
```

**分工原则**：

| 组件 | 输入字段 | 职责 |
|------|---------|------|
| Embedding（向量） | `title + problem_description + alert_info + root_cause` | 语义精确匹配（Precision） |
| tsv（BM25） | `title + content_md`（全文） | 关键字广召回（Recall） |
| `hit_count` | 历史数据 | 领域先验排序 |

---

## 六、核心组件：重排序（Reranking）

### 两阶段检索

```
第一阶段：向量/BM25 快速召回（Recall）
  → 从 10万 条记录召回 Top-50（毫秒级，近似搜索）

第二阶段：Cross-Encoder 精确排序（Precision）
  → 对 50 条精确计算 query-document 相关性得分（慢但准）
  → 取 Top-5 注入 LLM
```

**Bi-Encoder vs Cross-Encoder 的本质区别**：

```
Bi-Encoder（当前 embedding 模式）：
  embed(query)  → q_vec
  embed(doc)    → d_vec
  score = cosine(q_vec, d_vec)
  ← query 和 doc 独立编码，无交互，可预计算

Cross-Encoder（reranker）：
  score = model(concat(query, doc))
  ← query 和 doc 联合编码，注意力可以跨两者交互
  ← 准确率更高，但无法预计算 doc 的向量
```

---

## 七、核心组件：上下文注入与生成

### Context Window 管理

```
LLM prompt = [System] + [Retrieved Docs] + [Chat History] + [User Query]
```

**分工**：
- `content_md`（含视觉描述）→ LLM prompt（完整语义上下文注入）
- 章节字段 → Agent 推理基本单元（精确字段访问）

### Lost in the Middle 问题

研究发现 LLM 对 prompt 中间位置的内容注意力最弱：

```
Position:  [开头] [中间] [结尾]
Attention:  高      低     高

→ 最重要的 chunk 应该放在开头或结尾
```

---

## 八、高级技术

### HyDE（Hypothetical Document Embeddings）

解决 query 和 document 语义形式不匹配问题：

```python
# 让 LLM 先"假设"一个答案，用假设答案去搜索（形式更接近文档）
hypothetical_answer = llm("假设这个问题的答案是什么文档：" + query)
results = vector_search(embed(hypothetical_answer))
```

### Query Decomposition（查询分解）

```python
# 用户复杂查询 → 分解为多个子查询 → 各自检索 → 合并结果
query = "Redis 超时导致虚拟机0x0CFFFFFF且数据库报错"
sub_queries = ["Redis 超时", "虚拟机 0x0CFFFFFF", "数据库连接失败"]
```

### Contextual Retrieval（Anthropic 2024）

在分块时，给每个 chunk 加入文档级上下文前缀（由 LLM 生成）。Anthropic 报告这个方案将检索失败率降低 **49%**。

---

## 九、RAG 评估框架

| 维度 | 问题 | 指标 |
|------|------|------|
| **Faithfulness** | 回答有没有幻觉 | 答案中每句话能否在 retrieved docs 中找到依据 |
| **Answer Relevance** | 回答是否回答了问题 | query 与 answer 的语义相似度 |
| **Context Relevance** | 检索到的文档是否相关 | retrieved docs 与 query 的相关度 |

$$\text{End-to-End 质量} = f(\text{Context Relevance} \times \text{Faithfulness} \times \text{Answer Relevance})$$

三者的短板效应：任何一个弱，整体就弱。

**RAGAS** 是目前最广泛的 RAG 自动评估框架，用 LLM 作为评估器自动计算上述三个指标。

---

## 十、本项目 KBD RAG 实现映射

### 字段职责分离（优化后）

| 字段 | 数据来源 | 用途 |
|------|---------|------|
| `problem_description` / `alert_info` / `root_cause` / `title` | pipeline 提取，admin 可编辑 | Embedding 输入（问题空间语义向量） |
| `content_md` | 章节 + `images_json` 展开重建 | LLM 上下文注入（含视觉描述） |
| `tsv` | `title + content_md`（全文） | BM25 关键字广召回 |
| `embedding` | `embed(title + problem_description + alert_info + root_cause)` | 向量语义精确匹配 |
| `images_json` | pipeline Vision LLM | 图片视觉描述结构化存储 |

### 图片处理方案（方案 B）

章节字段中图片用 `![img:N]` 占位符标记位置，视觉描述独立存储在 `images_json`：

```json
[
  {"seq": 0, "section": "steps_text", "desc": "TYPE: 日志截图\nFULL_TEXT: ..."},
  {"seq": 1, "section": "problem_description", "desc": "TYPE: 告警截图\n..."}
]
```

`rebuild_content_md()` 读取 `images_json`，将 `![img:N]` 展开为完整的 `> **【截图说明】**` 块，从而保证即使 admin 编辑章节字段，视觉描述也不会丢失。

### 检索流程

```
用户问题 (query)
      ↓
┌─────────────────────────────────┐
│  向量检索（embedding 相似度）    │  ← embed(query) vs embed(问题侧字段)
│  BM25 检索（tsv 全文）          │  ← 关键字精确匹配
└─────────────────────────────────┘
      ↓  RRF 融合 + hit_count 加权
Top-K 候选（按 similarity 降序）
      ↓
content_md → LLM prompt 注入       ← 含视觉描述的完整上下文
章节字段 → Agent 推理              ← root_cause / solution 精确访问
```
