---
status: frozen
category: archive
audience: all
last_updated: 2026-10-26
owner: team
---

# 归档批次：agent-external-2026-10

> 2026-10-26 文档瘦身 P1 批次的归档清单。**只读**：本目录文件不再修改、不再随工作循环演进。
> 依据 `docs/文档管理规范.md` §12.3：现行文档引用本目录时只摘取结论摘要，不作为主干依赖。

## 归档判据

一份文档能否充当「现行全量文档」，只取决于两条可验证的硬事实：

1. 它描述的实现**存在于本仓代码**，读者能对照代码逐条核实；
2. 它有**读者会依据它行动**（写代码、部署、排障）。

本批次 18 篇两条都不满足：它们描述的是**另一个仓库**（`ops-agent`）与**第三方框架**（`pydantic-ai`）的内部实现，本仓只能看到调用它们的那薄薄一层适配代码。把它们留在 `solution/` 下，会让读者把「别人家的设计」误当「本仓的现行权威」——这正是文档腐化里最隐蔽的一种。

## ops-agent/（12 篇）

| 项 | 事实 |
|---|---|
| 描述对象 | 独立仓库 `github.com/P3n9W31/ops-agent`，本仓只是集成方 |
| 本仓现行契约看哪里 | 适配器 `backend/agent-service/app/adapters/agents/ops/ops_agent_adapter.py`；路由决策 `backend/agent-service/app/adapters/agents/agent_router.py`；端口定义 `backend/agent-service/app/domain/agent_port.py` |
| 归档原因 | 该子项目的内核、模块、运行时、工具系统只在其自有仓演进；本仓镜像一份必然腐化 |
| 已发现的具体腐化 | `ops-agent手工测试计划.md`、`项目使用手册.md` 的流程时点已过，与当前 `ops_agent_adapter.py` 的实际调用方式不再对应 |
| 入链处理 | 唯一入链是 `docs/deploy/部署设计.md` 的 v1.19 记录，已改为指向本目录 |

> 提醒：如需 ops-agent 内核细节，应到该仓取；本仓不承担其内部设计的维护责任。

## pydantic-agent/（6 篇）

| 项 | 事实 |
|---|---|
| 描述对象 | 第三方框架 `pydantic-ai`，本仓依赖 `pydantic-ai[openai]==0.8.1` |
| 本仓现行实现 | `backend/agent-service/app/adapters/agents/pai/pai_agent_adapter.py` |
| 归档原因 | 6 篇是框架源码级调研（Agent 类、基类、ReAct 模式支持、Agent 模式对比、RAG 指南）。框架升级即失效，属「实时型」知识，应由依赖版本与官方文档承担，不手工维护 |
| 仍有价值的部分 | 其中「故障排查 Agent 设计（Graph + ReAct）」曾指导本项目选型，选型结论已并入 [agent设计.md](../../solution/agent/design/agent设计.md) 的架构选型段 |

## 本批次释放的规模

| 指标 | 数值 |
|---|---|
| 归档文件数 | 18 篇（ops-agent 12 / pydantic-agent 6） |
| 从 `solution/` 移出的行数 | `ops-agent` 8,174 行 + `pydantic-agent` 4,700 行 = **12,874 行** |
| `solution/agent/` 现行文档数变化 | 82 篇 → **64 篇**（不含 `events/`） |
| 新增失效链接 | 0（唯一入链已改指本目录） |
