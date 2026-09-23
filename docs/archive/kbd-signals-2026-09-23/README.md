---
status: archived
category: archive
audience: all
last_updated: 2026-09-23
owner: team
---

# KBD 与信号文档整理前快照

这里保存 2026-09-23 整理前的 17 份完整正文，用于恢复历史说明、样例与决策背景。相对链接已按原始仓库目标重定位；历史代码、路径和统计本身不代表当前实现。

当前规则请从 [KBD 与关键信号统一入口](../../solution/knowledge-base/README.md) 阅读。各阶段 events 历史记录没有被修改；本目录不是新一轮部署或现场验收记录。

## 迁移清单

| 整理前原文快照 | 当前文档 | 处理方式 |
|---|---|---|
| [知识库设计.md](solution/knowledge-base/知识库设计.md) | [知识库设计.md](../../solution/knowledge-base/知识库设计.md) | 重写现行总览 |
| [KBD证据诊断与CDD闭环设计.md](solution/knowledge-base/KBD证据诊断与CDD闭环设计.md) | [KBD证据诊断与CDD闭环设计.md](../../solution/knowledge-base/KBD证据诊断与CDD闭环设计.md) | 合并当前运行与调度规则 |
| [KBD审核发布操作指南.md](solution/agent/KBD审核发布操作指南.md) | [KBD审核与信号配置-使用手册.md](../../user-guide/KBD审核与信号配置-使用手册.md) | 归位审核手册 |
| [KBD批量任务状态迁移说明.md](solution/agent/KBD批量任务状态迁移说明.md) | [KBD批量任务状态迁移说明.md](../../solution/knowledge-base/KBD批量任务状态迁移说明.md) | 归位生产专项 |
| [KBD语义入口兜底诊断设计与需求.md](solution/agent/KBD语义入口兜底诊断设计与需求.md) | [KBD语义检索与案例推荐设计.md](../../solution/knowledge-base/KBD语义检索与案例推荐设计.md) | 归档旧正文，原路径跳转 |
| [关键信号多Agent分层抽取与建模模板库设计.md](solution/agent/02-架构设计/关键信号多Agent分层抽取与建模模板库设计.md) | [关键信号抽取与建模设计.md](../../solution/knowledge-base/关键信号抽取与建模设计.md) | 归档旧正文，原路径跳转 |
| [KBD无信号案例推荐与诊断能力分离.md](solution/agent/KBD无信号案例推荐与诊断能力分离.md) | [KBD语义检索与案例推荐设计.md](../../solution/knowledge-base/KBD语义检索与案例推荐设计.md) | 归档旧正文，原路径跳转 |
| [KBD语义入口兜底诊断简明说明.md](solution/agent/KBD语义入口兜底诊断简明说明.md) | [README.md](../../solution/knowledge-base/README.md) | 归档旧正文，原路径跳转 |
| [KBD主动诊断信号设计.md](solution/knowledge-base/KBD主动诊断信号设计.md) | [KBD证据诊断与CDD闭环设计.md](../../solution/knowledge-base/KBD证据诊断与CDD闭环设计.md) | 归档旧正文，原路径跳转 |
| [在线与离线诊断模式-KBD信号利用路径对比.md](solution/agent/在线与离线诊断模式-KBD信号利用路径对比.md) | [KBD证据诊断与CDD闭环设计.md](../../solution/knowledge-base/KBD证据诊断与CDD闭环设计.md) | 归档旧正文，原路径跳转 |
| [关键信号架构落地设计.md](solution/agent/关键信号架构落地设计.md) | [关键信号架构设计.md](../../solution/knowledge-base/关键信号架构设计.md) | 归档旧正文，原路径跳转 |
| [QKV_QFK信号配置操作指南.md](solution/agent/02-架构设计/QKV_QFK信号配置操作指南.md) | [KBD审核与信号配置-使用手册.md](../../user-guide/KBD审核与信号配置-使用手册.md) | 归档旧正文，原路径跳转 |
| [QKV_QFK信号模型v2参考.md](solution/agent/02-架构设计/QKV_QFK信号模型v2参考.md) | [关键信号架构设计.md](../../solution/knowledge-base/关键信号架构设计.md) | 归档旧正文，原路径跳转 |
| [关键信号基类设计.md](solution/agent/02-架构设计/关键信号基类设计.md) | [关键信号架构设计.md](../../solution/knowledge-base/关键信号架构设计.md) | 归档旧正文，原路径跳转 |
| [关键信号架构迁移指南.md](solution/agent/02-架构设计/关键信号架构迁移指南.md) | [关键信号架构设计.md](../../solution/knowledge-base/关键信号架构设计.md) | 归档旧正文，原路径跳转 |
| [关键信号字段级分别抽取.md](solution/agent/02-架构设计/关键信号字段级分别抽取.md) | [关键信号抽取与建模设计.md](../../solution/knowledge-base/关键信号抽取与建模设计.md) | 归档旧正文，原路径跳转 |
| [关键信号数据模型分层重构RFC.md](solution/agent/02-架构设计/关键信号数据模型分层重构RFC.md) | [关键信号架构设计.md](../../solution/knowledge-base/关键信号架构设计.md) | 归档旧正文，原路径跳转 |

## 恢复与引用

- 原文件正文在本目录完整保留，未删除历史内容；可按迁移表找到原文。
- 原访问路径保留兼容跳转及章节定位；新文档应直接引用当前维护点。
- 引用历史决策应说明时间和当时约束，不把旧配置示例当成当前 Schema。
- 后续修订进入现行文档，不继续编辑这批冻结快照。
