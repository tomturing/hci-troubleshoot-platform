---
status: active
category: solution
audience: all
last_updated: 2026-07-30
owner: team
update_trigger: Agent 方案新增、重构或状态变更
---

# Agent 方案文档索引

> HCI 智能排障平台 Agent 层的设计、模版、测评与 GitOps 全生命周期方案。

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|---|---|---|---|
| 2026-07-31 | v1.6 | 启动 KBD 正式专家复核并将最小 Replay manifest 写入运行审计：只保存不可变 revision、计划/环境/参数哈希和 Terminal Bridge artifact 查找键；明确 `replayable=false`，不把引用冒充为完整回放。 | [KBD 最小回放证据契约与正式专家复核启动方案](events/2026-07-31-KBD最小回放证据契约与正式专家复核启动方案.md) |
| 2026-07-31 | v1.5 | 完成 KBD 专家监督与运行效果最小数据闭环：原因码/删除原因、精确 KBD runtime revision、CDD 编译与逐 Signal outcome、Capability Gap/metrics/评估导出；可信身份与 Replay/Gold/Challenger 仍保持未完成边界 | [KBD 专家监督与运行效果数据闭环方案](events/2026-07-31-KBD专家监督与运行效果数据闭环方案.md) |
| 2026-07-30 | v1.4 | 完成 KBD 专家主路径信息分层、独立维护工作稿和 Agent Capability Runtime Discovery；明确静态 Validation、未认证审核身份与真实 Expert Gold/replay 的边界 | [KBD 专家复核与全生命周期闭环方案](events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |
| 2026-07-30 | v1.3 | 补齐 hci-real/hci-sim 双轨、Go SSH 容器运行时、K3s 独立部署与 100+ Scenario 并发设计导航 | [HCI 真实环境与 hci-sim 双轨运行时设计](events/2026-07-30-HCI真实环境与hci-sim双轨运行时设计.md) |
| 2026-07-29 | v1.2 | 新增 HCI 6.11.1_R1 + aCLI 1.0.0 实机知识基线，明确日志、配置、数据、补丁、容器、aCLI 与 QKV/QFK 的事实边界和待确认演进 | [HCI 底层目录、日志、容器与 aCLI 知识基线](02-架构设计/HCI底层目录日志容器与aCLI知识基线.md) |
| 2026-07-29 | v1.1 | 新增 KBD 专家复核、不可变版本、Capability Registry 与全生命周期闭环方案导航 | [KBD 专家复核与全生命周期闭环方案](events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |

## 核心原则：测评先行

**测评不是 Agent 开发完成后的验证步骤，而是贯穿全生命周期的设计约束。**

```
传统方式:  开发 Tool/Skill/SOP → 手动测一下 → 上线 → 用户投诉 → 修
测评先行:  先写测评用例 → 开发时边写边跑 → 通过率达标才上线 → CI 自动回归 → Bad Case 回流
```

| 阶段 | 测评先行的含义 |
|------|--------------|
| **设计** | 定义 Tool/Skill/SOP 的同时定义"什么叫做好了"——预期 Trace、预期输出、预期效率基线 |
| **开发** | 每完成一个分支/步骤，立刻跑对应的测评用例。通过率从 20% 逐步提升到 100% |
| **审查** | PR 合并前 CI 先检查"有没有测评用例"（新增资源无用例 → 直接阻断），再跑受影响用例。退化 > 10% 直接阻断 |
| **发布** | PreSync Hook 校验 SOP 引用的 Tool/Skill 都存在且启用，校验失败则阻断部署不影响到线上。部署后 30 分钟自动观察 Agent 关键指标（成功率、延迟、SOP 命中率），异常时告警或回滚 |
| **运维** | 线上 Bad Case 自动提取 → 补充测评用例 → 纳入回归套件。同样的错误不会出现第二次 |

> 参考：[AI Agent & Skill 测评方案及落地实践](https://mp.weixin.qq.com/s/PUbGqheJhFMmb6hGj1ZtOw) — "能力测评是和 Skill 开发同步启动的，Skill 开发人员需要在设计之初就确定测评集，在开发过程中不断测评，通过测评结果优化 Skill"

## 目录结构

```
docs/solution/agent/
├── README.md                          ← 本文件
│
├── 01-模版与规范/                      ← 给 SOP/Tool/Skill 作者
│   ├── agent-resource-模版.md          Tool/Skill YAML 字段说明、SOP Markdown 规范、变量声明表
│   └── agent-能力边界与演进方向.md      Tool/Skill/SOP/ReAct/Agent 五层能力现状与扩展 Roadmap
│
├── 02-架构设计/                        ← 当前架构的权威设计文档
│   ├── agent设计.md                    整体设计
│   ├── agent工具设计.md                工具系统设计
│   ├── HCI底层目录日志容器与aCLI知识基线.md HCI 实机事实、aCLI 契约与能力演进
│   ├── qfk_log统一日志采集解析与判定设计.md 126 KBD 日志、Catalog、parser/predicate 与安全契约
│   ├── agent技能设计.md                技能系统设计
│   ├── agent记忆设计.md                记忆与变量池设计
│   ├── agent基类设计.md                Agent 基类设计
│   ├── 排障Agent可靠性整体解决方案.md    可靠性方案
│   ├── Agent可靠性三方案对比分析.md      可靠性方案对比
│   ├── S0意图识别与Prompt解耦设计方案.md S0 意图识别
│   ├── 变量池获取策略架构深度分析.md     变量池 JIT 策略
│   ├── sop决策树与滑动窗口机制实效分析.md SOP 滑动窗口
│   ├── prompt设计与加载机制分析.md       Prompt 设计
│   ├── prompt数据库化管理及动态热生效方案.md Prompt DB 管理
│   ├── skill调用失效根因分析与改进方案.md  Skill 失效分析
│   ├── skill调用失效改进后恶化根因与闭环方案.md Skill 失效闭环
│   ├── SSH终端代理(terminal_bridge)架构分析与演进方案.md Terminal Bridge
│   ├── acli插件工具命令模板机制重新设计方案.md ACLI 模板
│   ├── 大脑可选-集成重设计方案.md        AI 大脑选择
│   ├── 案例差异诊断协议.md              案例诊断
│   ├── 关键信号抽取问题分析与修复方案.md KBD 信号抽取历史问题
│   ├── 智能体单步交互与单步命令执行控制方案.md 单步控制
│   └── ops-agent与hci-troubleshoot-platform关系分析.md Ops-Agent 关系
│
├── 03-测评与GitOps/                    ← 测评方法论、CI 门禁、GitOps 全生命周期
│   └── agent-测评与GitOps方案.md
│
├── events/                            ← 历史设计事件（归档）
├── ops-agent/                         ← Ops-Agent 外部子项目
└── pydantic-agent/                    ← pydantic-ai 框架分析（参考）
```

## 快速导航

| 我要做什么 | 看哪个 |
|-----------|--------|
| 写一个新的 SOP/Tool/Skill | [agent-resource-模版.md](01-模版与规范/agent-resource-模版.md) |
| 了解某个能力为什么不支持、有什么替代方案 | [agent-能力边界与演进方向.md](01-模版与规范/agent-能力边界与演进方向.md) |
| 搭建 CI 测评门禁 | [agent-测评与GitOps方案.md](03-测评与GitOps/agent-测评与GitOps方案.md) |
| 理解 Agent 整体架构 | [agent设计.md](02-架构设计/agent设计.md) |
| 理解工具系统怎么工作 | [agent工具设计.md](02-架构设计/agent工具设计.md) |
| 核对 HCI 日志/配置/数据/容器与 aCLI 的真实契约 | [HCI底层目录日志容器与aCLI知识基线.md](02-架构设计/HCI底层目录日志容器与aCLI知识基线.md) |
| 设计或审核 qfk_log、blackbox、日志 parser/predicate | [qfk_log统一日志采集解析与判定设计.md](02-架构设计/qfk_log统一日志采集解析与判定设计.md) |
| 理解技能系统怎么工作 | [agent技能设计.md](02-架构设计/agent技能设计.md) |
| 理解记忆与变量池怎么工作 | [agent记忆设计.md](02-架构设计/agent记忆设计.md) |
| 理解可靠性方案 | [排障Agent可靠性整体解决方案.md](02-架构设计/排障Agent可靠性整体解决方案.md) |
| 理解 S0 意图识别怎么路由 | [S0意图识别与Prompt解耦设计方案.md](02-架构设计/S0意图识别与Prompt解耦设计方案.md) |
| 排查 Skill 调用失败问题 | [skill调用失效根因分析与改进方案.md](02-架构设计/skill调用失效根因分析与改进方案.md) |
| 理解变量是怎么获取的 | [变量池获取策略架构深度分析.md](02-架构设计/变量池获取策略架构深度分析.md) |
| 设计 KBD 驱动 hci-sim、real/sim 双轨校准和 100+ Agent 并发回归 | [Agent 并发测试与 KBD 驱动 HCI 仿真环境方案](Agent并发测试与KBD驱动HCI仿真环境方案.md) |
| 理解 KBD 截图、关键信号与案例验证的目标架构 | [KBD 截图证据、关键信号与可执行诊断契约方案](events/2026-07-28-KBD截图证据与可执行诊断契约方案.md) |
| 设计 KBD 专家复核、模型/专家双轨版本、发布生命周期与 Capability 闭环 | [KBD 专家复核、版本治理与生产消费闭环方案](events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |
| 理解 SOP 决策树与滑动窗口 | [sop决策树与滑动窗口机制实效分析.md](02-架构设计/sop决策树与滑动窗口机制实效分析.md) |
| 了解历史设计决策背景 | [events/](events/) |
