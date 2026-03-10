# HCI 智能排障平台 - AI 层与 RAG 层对接架构决策

## 文档信息

- **版本**: 1.0
- **作者**: Claude
- **创建日期**: 2026-03-09
- **状态**: 决策已定 — 三阶段演进路线确立
- **决策类型**: 架构决策记录（ADR）

---

## 文档边界

- **本文档范围**：AI Assistant Pod Pool 与 RAG 层（KB Service）之间的**对接模式**——知识由谁拉取、何时拉取、以何种方式注入、如何演进。
- **不在本文档**：KB Service 内部实现（见 `13_RAG设计.md`）；AI Agent 行为规范（见 `12_AI层设计.md`）。
- **相关文档**：`12_AI层设计.md` — AI Agent 能力规范；`13_RAG设计.md` — 知识库基础设施；`01_架构设计.md` — 平台整体架构。

---

## 一、背景与问题定义

### 1.1 触发原因

在分析如何让 ProductionClaw 使用 SOP 知识库时，发现当前架构（`conversation-service` 外挂 RAG 注入）与 openclaw 的真实能力定位存在根本冲突：

**openclaw 的真实身份**（来自官方 README + 本地 `docs.acp.md`）：

| 能力 | 描述 |
|---|---|
| Pi agent runtime | 完整 tool-calling 循环，不是被动问答引擎 |
| `sessions_*` 工具 | Agent-to-Agent 通信（`sessions_send`） |
| Workspace skills | `AGENTS.md` / `SOUL.md` / `TOOLS.md` 定义行为 |
| ACP bridge | 外部服务通过 session key 路由到指定 agent |
| 多 agent 路由 | 不同 session namespace 对应不同 agent |

**当前（有问题的）架构**：

```
用户消息
  → conversation-service（做 RAG 编排）
    ├── POST /api/kb/sop/match
    ├── POST /api/kb/search
    └── 拼装 4-Tier Prompt → POST /v1/chat/completions
                                  ↓
                            openclaw（被当成被动 LLM）
```

openclaw 只看到 system prompt + 用户消息（RAG 上下文预烘焙为纯文本）。它：
- ❌ 不知道 KB Service 存在，无法自主决策何时检索
- ❌ 无法多跳检索（第一次搜到 A，发现需要搜 B）
- ❌ `productionclaw/TOOLS.md` 中定义的 KB 调用规范形同虚设

### 1.2 核心决策问题

> **ProductionClaw 应该主动拉取知识，还是被动接收注入？**

这是 AI 层行为的根本性设计决策，影响：
- `conversation-service` 的职责定位（RAG 编排者 vs 纯消息路由器）
- ProductionClaw 的 agent loop 是否得到完整利用
- LearningClaw → ProductionClaw 的自主学习闭环是否能够实现
- 未来 LLM 对照组的接入方式与对比公平性

---

## 二、约束条件

在评估方案前，以下约束必须明确：

| 约束 | 说明 |
|---|---|
| openclaw 是全能 Agent | 不是可以随意替换的 LLM 接口；有完整 agent loop |
| `productionclaw/TOOLS.md` 已定义 KB 调用规范 | 这是设计意图，不应被架构绕过 |
| LearningClaw 写 KB，ProductionClaw 读 KB | 职责分离已定，读取路径需要满足此设计 |
| kb-service 当前未部署 | `values.yaml kbService.enabled: false`，三个阶段都需要它 |
| SOP 是权威静态知识 | 更新慢、结构化，适合本地化；不适合每次通过 HTTP 检索 |
| 需要支持 LLM 对照组 | 评估框架必须能公平对比 openclaw 与纯 LLM |

---

## 三、三阶段演进方案

> **结论先行**：三个阶段是**线性递进**关系，不是平行可选的三条路。阶段三是目标态，阶段二是必经阶段，阶段一是最小可用起点。

### 阶段一：修复现状（外挂 RAG，可运行）

**核心变化**：部署 kb-service，让现有 4-Tier Prompt 注入**实际工作起来**。

**架构图**：

```
用户消息
  ↓
conversation-service（RAG 编排者角色）
  ├── POST /api/kb/sop/match    → SOP 全文注入（Tier-2）
  ├── POST /api/kb/search       → 语义检索 top-k（Tier-3）
  └── 拼装完整 system prompt
          ↓
      POST /v1/chat/completions（stream=true）
          ↓
      ProductionClaw（被动接收上下文，作为 LLM 使用）
          ↓
      返回流式文本
```

**实施内容**：
1. 部署 kb-service（`helm values.yaml: kbService.enabled: true`）
2. 构建并导入 kb-service Docker 镜像到 k3s
3. 摄入 SOP 文档（`POST /api/kb/ingest`）
4. 摄入已有 Markdown 案例（可选，视时间）
5. 验证 conversation-service 的 RAG 注入端到端链路

**优点**：
- 本周内可完成，最快让系统可用
- 完全不改 openclaw 侧
- 阶段一的外挂 RAG 是 LLM 对照组**最干净的基准对比数据**来源（见第四节）

**缺点**：
- openclaw 能力浪费：`TOOLS.md` 中定义的 KB 工具没有实际使用
- 不支持多跳检索
- ProductionClaw 的 BOOTSTRAP 阶段（`§4.5 知识获取流程`）仍然需要外部注入驱动

**阶段一完成标准**：
- [ ] `kubectl get pods -n hci-troubleshoot` 中 kb-service 处于 Running
- [ ] `POST /api/kb/search {"query": "虚拟机启动失败"}` 返回有效 chunks
- [ ] `POST /api/kb/sop/match {"query": "虚拟机无法启动"}` 命中 vm-power-failure SOP
- [ ] 一次完整对话：用户提问 → SOP 被正确注入 → ProductionClaw 基于 SOP 回答
- [ ] Grafana/Loki 中可见 `kb_chunks_used > 0` 的日志

---

### 阶段二：Agentic RAG（ProductionClaw 主动工具调用）

**核心变化**：`conversation-service` 从"RAG 编排者"降级为"消息路由器"，ProductionClaw 通过 `TOOLS.md` 中定义的工具**自主**决定何时、搜什么。

**架构图**：

```
用户消息
  ↓
conversation-service（消息路由器 + session 管理）
  └── 路由消息到 ProductionClaw Pod
          ↓
      ProductionClaw（Pi agent runtime，完整 tool-calling 循环）
        ├── 读取 TOOLS.md（已定义 KB Service URL + 调用规范）
        ├── 判断：需要检索 → 自主 POST /api/kb/search
        ├── 判断：需要 SOP → 自主 POST /api/kb/sop/match
        ├── 看结果 → 判断是否需要多跳检索
        └── 生成排障回复
          ↓
      返回流式文本
```

**与阶段一的差异**：

| 维度 | 阶段一 | 阶段二 |
|---|---|---|
| 谁决定搜什么 | conversation-service（盲目，不了解对话语义） | ProductionClaw（了解完整对话上下文） |
| 多跳检索 | ❌ 一次注入 | ✅ 自主判断是否继续检索 |
| `TOOLS.md` 使用 | ❌ 形同虚设 | ✅ 完整生效 |
| conversation-service 职责 | RAG 编排 + 消息路由 | 纯消息路由 + session 管理 |

**实施内容**：
1. 修改 `conversation-service`：移除 KB 检索调用，只保留消息存储 + Pod 路由
2. 确保 ProductionClaw Pod 网络可访问 `http://kb-service:8004`（同命名空间，无需额外配置）
3. 验证 ProductionClaw 的 BOOTSTRAP 阶段自主调用 KB（`§4.5 知识获取流程`）
4. 验证对话中按需补充检索

**前置依赖**：阶段一完成（kb-service 已部署且有数据）

**完成标准**：
- [ ] `conversation-service` 代码中无 `kb_service.search` / `kb_service.match_sop` 调用
- [ ] ProductionClaw 的 openclaw 日志中可见工具调用记录
- [ ] 一次多跳排障对话：初始检索→用户补充信息→二次检索→缩小范围

---

### 阶段三：混合架构（SOP Workspace Skills + 动态 KB 检索，目标态）

**核心变化**：SOP 文档从"每次通过 HTTP 检索"进化为"挂载进 ProductionClaw Pod 的本地 workspace skill"；动态案例检索仍走工具调用；LearningClaw 更新 skills 实现自主学习闭环。

**知识分层原则**：

| 知识类型 | 特点 | 存储位置 | 访问方式 |
|---|---|---|---|
| SOP 文档 | 权威、结构化、更新慢 | `workspace/skills/` ConfigMap 挂载 | Pod 本地文件，零延迟，无 HTTP |
| 产线历史案例 | 非结构化、量大、需检索 | KB Service（pgvector） | tool-calling，按需检索 |
| Session 上下文 | 临时、对话相关 | `session-memory`（已在 AGENTS.md 定义） | Session 内直接访问 |

**架构图**：

```
┌────────────────────────────────────────────────────────┐
│                  ProductionClaw Pod                    │
│                                                        │
│  workspace/skills/                                     │
│    ├── vm_power_failure/                               │ ← ConfigMap 挂载
│    │     ├── keywords_map.json                         │   SOP 本地化，零延迟
│    │     └── chapters/*.md                             │
│    ├── storage_offline/                                │
│    └── node_degraded/                                  │
│                         ↑                              │
│              LearningClaw 定期更新 ConfigMap            │
│                                                        │
│  Pi agent runtime                                      │
│    ├── 启动时：读本地 skills（SOP，零延迟，无 HTTP）      │
│    ├── 对话中：工具调用 KB Service（案例库，新鲜检索）    │
│    └── 结案时：sessions_send → LearningClaw 提炼经验    │
│                                                        │
└──────────────┬─────────────────────┬───────────────────┘
               │ HTTP 工具调用        │ sessions API
               ▼                     ▼
        KB Service（案例库）    LearningClaw Pod
               ↑
        产线结案 / 网络爬取
```

**SOP 进入 Workspace Skills 的配置方式**：

```yaml
# deploy/helm/hci-platform/values.yaml
productionClaw:
  workspaceSkills:
    enabled: true
    configMaps:
      - name: sop-skills-vm-power-failure
        mountPath: /home/node/.openclaw/workspace/skills/vm_power_failure/
      - name: sop-skills-storage-offline
        mountPath: /home/node/.openclaw/workspace/skills/storage_offline/
```

源数据来自已存在的 `data-pipeline/sop_skills/` 目录，通过 CI/CD 打包为 ConfigMap。

**LearningClaw 自主学习闭环**：

```
结案工单
  → Conversation Service 发事件
  → LearningClaw：提炼对话经验
      ├── 写入 KB Service（新案例）
      └── 判断是否需要更新 SOP Skill
            ↓（如需更新）
          生成新版 skill 内容
          → kubectl create/replace configmap sop-skills-*
          → kubectl rollout restart deployment/productionclaw-pool
```

**前置依赖**：阶段二完成；openclaw workspace skills 格式验证

**完成标准**：
- [ ] ProductionClaw Pod 启动时 `workspace/skills/vm_power_failure/` 可读
- [ ] vm_power_failure 相关问题无需 HTTP 调用即可获取 SOP 内容
- [ ] LearningClaw 结案后可写入新案例（KB Service 摄入接口）
- [ ] 完整的学习闭环：一次真实工单 → LearningClaw 提炼 → KB 中可检索到

---

## 三·附、三阶段横向对比

### 综合对比矩阵

| 评估维度 | 阶段一：外挂 RAG | 阶段二：Agentic RAG | 阶段三：混合架构 |
|---|---|---|---|
| **openclaw 能力利用** | ❌ 完全浪费 | ✅ 完整 | ✅ 最充分 |
| **实施难度** | ★ 最简单 | ★★★ 中等 | ★★★★★ 最复杂 |
| **上线时间** | 本周 | 2-3 周 | 1-2 月 |
| **多跳推理** | ❌ | ✅ | ✅ |
| **SOP 读取延迟** | 中（HTTP 往返） | 中（tool-calling） | 低（本地文件，零延迟） |
| **自主学习闭环** | ❌ | 部分 | ✅ 完整 |
| **确定性 / 可调试** | ✅ 最高 | 中 | 中 |
| **与现有设计对齐度** | 低（TOOLS.md 被绕过） | 高（TOOLS.md 得到满足） | 最高（完整实现设计意图） |

### 阶段三（混合架构）详细评估

**优点**

| 维度 | 评分 | 说明 |
|---|---|---|
| openclaw 能力利用率 | ★★★★★ | Skills + Tools + 全 agent loop，能力全部激活 |
| SOP 读取延迟 | ★★★★★ | 零延迟（ConfigMap 本地文件），完全不依赖 kb-service |
| 自主学习闭环 | ★★★★★ | LearningClaw → skills 更新 → ProductionClaw 自动获益 |
| 排障质量 | ★★★★★ | SOP 保证专业深度 + 案例库保证经验广度，双保险 |
| 知识分层清晰 | ★★★★★ | 权威知识（SOP skills）vs 经验知识（KB 案例），各司其职 |

**缺点**

| 维度 | 评分 | 说明 |
|---|---|---|
| 实施复杂度 | ★★ | 需要 ConfigMap 打包 + skills 格式规范 + LearningClaw 同步逻辑 |
| Pod 启动时间 | ★★ | skills 目录挂载比 HTTP 拉取慢（但仅一次性，后续对话零延迟） |
| openclaw skills 格式兼容性 | 待验证 | 需确认 openclaw workspace skills 的确切文件格式与加载机制 |

> **结论**：阶段三是唯一真正发挥 openclaw 全部能力的架构，整体质量最高，但需要先在阶段二验证工具调用稳定性才可进入。

---

## 四、LLM 对照组接入设计

### 4.1 架构位置

LLM 作为 AI Assistant Pod Pool 的一种新类型接入，与 openclaw-pool 并列：

```
AI Assistant Pod Pool
  ├── productionclaw-pool   （openclaw Agent + 完整工具调用）
  ├── llm-deepseek-pool     ← 新增（纯 LLM，外挂 RAG）
  └── llm-qwen-pool         ← 新增（纯 LLM，外挂 RAG，可选）
```

接入方式：LLM 原生兼容 `POST /v1/chat/completions`，在 AssistantRegistry 中添加一条配置即可，**零代码修改**：

```yaml
# ASSISTANT_REGISTRY 新增条目
llm-deepseek:
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"
  warm_pool_size: 0    # 无状态，不需要 K8s Pod 热备
  health_check: "get_health"
  display_name: "DeepSeek（对照组）"
  description: "纯 LLM 对照，用于验证 openclaw Agent 增益"
```

### 4.2 对比公平性设计

> **关键原则：只有在相同知识获取条件下的对比才有意义。**

| 对比场景 | openclaw 获得什么 | LLM 对照组获得什么 | 比较的是什么 | 是否公平 |
|---|---|---|---|---|
| **阶段一对比（推荐基准）** | conversation-service 注入的 4-Tier Prompt | 完全相同的 4-Tier Prompt | 纯模型回答质量（推理、表达、安全性） | ✅ 公平 |
| 阶段二：LLM 被动接收 | openclaw 自主工具调用检索 | conversation-service 注入（被动） | 不对等 —— Agent 策略 vs 被动接收 | ❌ 不公平 |
| 阶段二：LLM 配 function calling | openclaw 工具调用 | LLM function calling + 同一 KB API | Agent 工具使用策略对比 | ✅ 公平，但需额外开发 |

**结论**：
- **阶段一**是建立基准数据的最佳时机，此时两者接受完全相同的上下文
- **阶段二以后**，若要公平对比，LLM 对照组需要配置同等的 function calling 能力（额外工作量约 1-2 天）
- 评估指标已有基础设施：`assistant_evaluation` 表 + `assistant_type` 字段，开启 A/B 分配即可采集数据

### 4.3 评估指标说明

`assistant_evaluation` 表已支持以下对比维度：

| 指标 | 字段 | 说明 |
|---|---|---|
| 解决率 | `resolved` | 工单是否成功关闭 |
| 用户满意度 | `user_rating` (1-5) | 工程师主观评分 |
| 解决效率 | `resolution_time_sec` | 从开始到关闭的时间 |
| 对话轮数 | `message_count` | 更少轮数 = 更精准 |
| Assistant 类型 | `assistant_type` | `productionclaw` vs `llm-deepseek` 等 |

Admin UI 助手评估看板将基于上述数据生成对比视图（待开发）。

---

## 五、实施路线图

```
2026-03-09  ←── 当前节点
     │
     ├── [阶段一] 本周内完成（约 2-3 天）
     │     ├── 部署 kb-service
     │     ├── 摄入 SOP 文档
     │     └── 验证端到端 RAG 链路
     │           ↓
     │     [可用基准测试点]：在此时接入 LLM 对照组，采集阶段一公平基准数据
     │
     ├── [阶段二] 约 2-3 周后
     │     ├── conversation-service 降级为消息路由器
     │     ├── ProductionClaw 工具调用 KB Service
     │     └── 验证多跳检索
     │           ↓
     │     [若要继续公平对比]：为 LLM 对照组配置 function calling
     │
     └── [阶段三] 约 1-2 月后（可选）
           ├── SOP → ConfigMap → workspace/skills/ 挂载
           ├── LearningClaw 自动更新 skills
           └── 验证完整自主学习闭环
```

---

## 六、当前决策结果

| 决策点 | 结论 |
|---|---|
| 演进模式 | 三阶段线性递进，非平行可选 |
| 阶段一目标 | 最小可用：部署 kb-service + 现有外挂 RAG 跑通 |
| 阶段二目标 | ProductionClaw 自主工具调用，conversation-service 降级 |
| 阶段三目标 | SOP 本地化 + 自主学习闭环（目标态） |
| LLM 对照组位置 | AI 层（AssistantRegistry 新类型），不在 RAG 层 |
| 基准对比时机 | 阶段一完成后，条件最公平 |
| 对比公平性原则 | 相同知识获取路径下才可对比模型能力差异 |

**当前状态**：阶段一实施中（2026-03-09 开始）

---

*文档版本: 1.0 | 创建: 2026-03-09 | 更新: 2026-03-09*
