# Code Citations

## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b + b \cdot \frac{|D|}
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b + b \cdot \frac{|D|}
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b + b \cdot \frac{|D|}
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

$$\text{score}(D,Q) = \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i,D) \cdot (k_1+1)}{f(q_i,D) + k_1 \cdot (1 - b + b \cdot \frac{|D|}{\text{avg
```


## License: GPL-3.0
https://github.com/eventh/tdt4215/blob/8e9b0f3285c91b48ab75023aadce595689ed1ffe/report/method.tex

```
## RAG 技术深度解析

---

## 一、第一性原理：为什么需要 RAG

### LLM 的本质局限

LLM 是**压缩的世界知识**，本质上是一个函数：

$$P(\text{answer} \mid \text{question, params})$$

训练时所有知识都被"压缩"进权重。这带来三个根本性缺陷：

| 缺陷 | 原因 | 举例 |
|------|------|------|
| **知识截止** | 训练数据有时间边界 | 不知道今天的故障 |
| **私有知识盲区** | 企业内部文档未参与训练 | 不知道你的 KBD 案例 |
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

这是你问题二的核心：

```
对称检索：query 和 document 来自同一分布
  query = "Redis 超时导致虚拟机启动失败"
  doc   = "Redis 连接超时引发虚拟机开机报错"
  → 用同一个 embedding 模型，效果好

非对称检索：query 短，document 长且与 query 形式不同
  query = "0x0CFFFFFF"（用户问题描述，短）
  doc   = 1000字的案例文档（长，包含背景/步骤/解决方案）
  → 需要专门的非对称模型（如 e5-mistral-instruct）
```

**你的系统是非对称场景**，但当前用的是通用 embedding。这是 embedding 质量的第一个改进点。

### 为什么 Embedding 字段选择很重要

Embedding 将文本压缩到定长向量，**不同字段对向量的贡献是加权叠加的**。

```
embed("问题描述 + 告警信息 + 解决方案")

= α × embed(问题描述) + β × embed(告警信息) + γ × embed(解决方案)
  (近似，实际是 transformer 的全局注意力)
```

如果 `solution` 占 40% 的 token，它会显著拉偏向量，使相同解决方案但不同问题的案例看起来相似。

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

-
```

