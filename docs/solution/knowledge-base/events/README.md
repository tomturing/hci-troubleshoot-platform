---
status: active
category: solution
audience: developer
last_updated: 2026-04-07
owner: team
---

# knowledge-base（知识库模块）方案事件文档

> 本目录存放 **knowledge-base（知识库模块）** 的方案事件文档，记录设计决策和方案选型历史。

---

## 文档列表

| 文件 | 日期 | 说明 |
|------|------|------|
| 2026-03-05-知识库RAG设计初版.md | 2026-03-05 | 知识库 RAG 设计初版 |
| 2026-03-05-DataPipeline-ETL设计方案.md | 2026-03-05 | Data-Pipeline ETL 设计方案（已归档，不再采用） |
| 2026-03-05-PageIndex对比分析.md | 2026-03-05 | PageIndex 对比分析（方案选型，决策：不采用） |
| 2026-03-27-知识库RAG设计v2.md | 2026-03-27 | 知识库 RAG 设计 v2（双轨三级 Fallback） |
| 2026-07-26-Q2026072624224-S0到S1知识路由与CDD失效分析.md | 2026-07-26 | S0 分类结果被二次检索推翻、CDD 无证据结论与自由 fallback 的现场复盘 |
| 2026-07-26-S0分类驱动的KBD证据诊断与CDD闭环设计.md | 2026-07-26 | S0 分类权威输入、KnowledgeSnapshot 和 KBD 证据闭环目标架构 |
| 2026-07-26-KBD主动诊断信号调度与证据闭环算法设计.md | 2026-07-26 | 分类全量 KBD 上的主动 acquisition 调度、候选状态机与 Conclusion Gate |

---

## 归档方案说明

以下方案已归档，不再采用：

| 方案 | 归档原因 | 现行替代 |
|------|---------|---------|
| Data-Pipeline ETL | 架构复杂，独立 ETL 管道维护成本高 | KB Service `/api/kb/ingest` 接口 |
| PageIndex | 索引成本极高，无增量更新能力 | SOP 关键字精确路由 + BM25/向量混合检索 |
| S1 query-based 三轨路由 | S0 已确认分类后仍用模糊 query 过滤分类资源，可能丢失真实 KBD | 分类 KnowledgeSnapshot + KBD 证据诊断 |

---

## 相关目录

- `../` - knowledge-base 主干文档（知识库设计.md）
- `2026-07-26-S0分类驱动的KBD证据诊断与CDD闭环设计.md` - S0 分类权威输入、KBD 证据状态机和 CDD 闭环目标架构
- `2026-07-26-KBD主动诊断信号调度与证据闭环算法设计.md` - 分类全量 KBD 上的主动信号调度、候选状态机与 Conclusion Gate
- `../../task/knowledge-base/events/` - 任务事件文档

---

*更新日期: 2026-07-26*
