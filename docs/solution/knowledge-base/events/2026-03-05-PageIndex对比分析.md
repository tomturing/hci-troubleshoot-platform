---
status: active
category: solution
audience: developer
last_updated: 2026-04-07
owner: team
---

# PageIndex 对比分析（方案选型）

> **归档说明**：本方案为 RAG 检索策略选型分析，对比 PageIndex 与混合检索方案。
> **决策结论**：不采用 PageIndex，借鉴其树形可解释性思路，采用 SOP 关键字精确路由 + BM25/向量混合检索。
> **归档日期**：2026-04-07

---

## 一、PageIndex 概述

PageIndex（VectifyAI）是一个无向量、纯 LLM 推理的 RAG 框架，核心理念："Similarity ≠ Relevance"。

---

## 二、方案对比

| 维度 | PageIndex | 本项目方案 |
|------|-----------|-----------|
| 索引方式 | LLM 逐节点生成摘要 → 树形索引 | Embedding + BM25 双路索引 |
| 检索方式 | LLM 从根节点推理遍历树 → 定位叶子 | 关键字精确匹配 + 向量/全文混合检索 |
| 核心理念 | 用 LLM 推理替代向量相似度 | SOP 确定性路由 + 语义兜底 |
| 文档结构 | PDF/MD → 自动检测 TOC → 生成树 | MD → YAML frontmatter + 正文分段 |
| 向量依赖 | 无（纯 LLM） | 有（pgvector） |

---

## 三、借鉴点

- 树形索引思路 → 用于 SOP skill 的层级路由（keywords_map.json）
- 可解释检索路径 → 返回 l1 > l2 > 章节名 命中路径给用户
- "不是所有问题都需要向量" → SOP 精确匹配不走向量

---

## 四、不采用的原因

| 问题 | 说明 |
|------|------|
| 索引成本极高 | 每节点 LLM 调用，7000 文档不可接受 |
| 不理解 SOP 流程依赖 | 步骤 1→2→3 的顺序逻辑无法表达 |
| 无增量更新能力 | 全量重建成本高 |
| 强 OpenAI 依赖 | 中文 HCI 领域适配差 |

---

## 五、最终决策

采用 **SOP 关键字精确路由 + BM25/向量混合检索** 组合：

1. **SOP 轨道优先**：keywords_map.json 精确匹配（O(n*m)，关键字 < 1000，可接受）
2. **KB 轨道兜底**：BM25 + Vector + RRF 融合
3. **category_id 直查**：S1+ 用 category_id 直接查 SOP（O(1)）

---

*归档日期: 2026-04-07*