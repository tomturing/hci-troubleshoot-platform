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

---

## 归档方案说明

以下方案已归档，不再采用：

| 方案 | 归档原因 | 现行替代 |
|------|---------|---------|
| Data-Pipeline ETL | 架构复杂，独立 ETL 管道维护成本高 | KB Service `/api/kb/ingest` 接口 |
| PageIndex | 索引成本极高，无增量更新能力 | SOP 关键字精确路由 + BM25/向量混合检索 |

---

## 相关目录

- `../` - knowledge-base 主干文档（知识库设计.md）
- `../../task/knowledge-base/events/` - 任务事件文档

---

*更新日期: 2026-04-07*