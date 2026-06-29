# ops-agent 与 hci-troubleshoot-platform 关系深度分析

> 分析日期：2026-04-28  
> 分析方法：第一性原理（从两个项目的原始代码出发，独立建立认知，不依赖既有结论）  
> 分析者：GitHub Copilot

---

## 一、核心结论（先读）

两个项目并非竞争关系，而是**同一问题在不同维度的解法**，在架构层面高度同构，在实现层面各有侧重，在关系层面存在明确的集成意图：

| 维度 | ops-agent | hci-troubleshoot-platform |
|------|-----------|--------------------------|
| **定位** | 独立AI排障引擎（可移植） | 企业级运维支撑平台（可扩展） |
| **关系** | AI 核心能力供体 | AI 能力的消费和放大器 |
| **核心价值** | SOP驱动的可解释推理 | 多租户、实时数据、团队协作 |
| **集成状态** | 已实现 OpenAI-compatible API | htp 已添加 OpsAgentAssistant 适配器 |

---

## 二、问题溯源：为什么是这两个项目？

### 2.1 同一问题的两种视角

两个项目都在解决同一个现实问题：**深信服 HCI 超融合平台的故障排查**太依赖经验丰富的现场工程师，知识无法沉淀和规模化复用。

但两个团队从不同入口切入：

**ops-agent 的入口：知识引擎**
> "如何让 AI 按照正确的 SOP 流程排查问题，而不是凭空推理？"

出发点是知识本身，解决的是**推理的正确性**问题——AI 的输出要有据可查，要可追踪，要符合规范操作手册。

**htp 的入口：平台产品**
> "如何让工程师在一个协作平台上，借助 AI 更高效地处理客户工单？"

出发点是业务流程，解决的是**能力的规模化和协作**问题——多人协作、实时数据、审计追踪、运营管理。

### 2.2 第一性原理推导出的必然关系

从问题出发，可以推导出：
- 一个真正实用的 HCI 排障平台**必须有**可靠的 AI 推理核心
- 一个可靠的 AI 推理核心**如果**被孤立在 CLI 中，价值会受限

两个项目的出现，本质是同一个需求在两个维度的分工：ops-agent 解决"想清楚"的问题，htp 解决"做到位"的问题。

---

## 三、架构同构分析

### 3.1 相似性：都实现了 ReAct 驱动的诊断流程

两个项目的核心执行引擎在结构上高度相似：

```
ops-agent: ReAct Loop
  Think → Act (tool_call) → Observe → Think → ...
  入口：AgentExecution.run() @ ops_agent/agent/execution.py
  上限：MAX_STEPS=500（长流程，允许 SOP 逐步引导）

htp conversation-service: ReactExecutor
  Think → Act (tool_call) → Observe → Think → ...
  入口：ReactExecutor.run() @ backend/conversation-service/app/core/react_executor.py
  上限：MAX_STEPS=15（短流程，单轮快速诊断）
```

**步数上限的差异揭示了设计取向的本质差异：**

ops-agent 的 500 步意味着它被设计为**长期对话引擎**——可以完整地引导用户走完一份长达数十步的 SOP，每个工具调用可能是一次人机交互（`get_info_from_user`）或知识查询（`query_sop`）。

htp 的 15 步意味着它被设计为**快速决策引擎**——每步调用的是真实的系统命令（`acli_service_restart`、`get_active_alerts`），速度快但深度浅。

### 3.2 相似性：都有声明式工具注册表

**ops-agent 工具注册（通过 @register_tool 装饰器）：**
```python
# ops_agent/tools/
@register_tool
def query_sop(query: str) -> str: ...         # SOP 查询

@register_tool  
def get_info_from_user(question: str) -> str: ... # 用户交互

@register_tool
def request_approval(action: str) -> bool: ...    # 高危操作审批
```

**htp 工具注册（通过 TOOL_REGISTRY dict）：**
```python
# backend/conversation-service/app/core/tool_registry.py
TOOL_REGISTRY = {
    "get_active_alerts": ToolDefinition(risk_level=1, category="scp"),
    "acli_service_restart": ToolDefinition(risk_level=2, category="acli"),
    "acli_netdoctor": ToolDefinition(risk_level=2, category="acli"),
    ...
}
```

两者都将工具定义从执行逻辑中解耦，体现了相同的架构直觉。但 htp 额外引入了 `risk_level` 字段（1=自动执行 / 2=需用户确认 / 3=完全阻止），这是 ops-agent 的 `approval_manager` 的更声明式的表达形式。

### 3.3 相似性：都有诊断阶段状态机

**ops-agent 状态机：**
```
intake → routing → validation → solution → confirmation
                                          ↓
                              reroute ←──┘└──→ escalation → closed
```

**htp 状态机（S0-S6）：**
```
S0(意图识别) → S1(故障定位) → S2(假设生成) → S3(验证执行)
                                              → S4(根因确认) → S5(方案输出) → S6(验证闭环)
                                              ↓
                                         S0_FAILED（触发人工升级）
```

两者不仅结构对应，连语义也基本一致：

| ops-agent | htp | 语义 |
|-----------|-----|------|
| intake | S0 | 接收问题，理解意图 |
| routing | S1 | 定位故障类型 |
| validation | S2/S3 | 形成假设，执行验证 |
| solution | S4/S5 | 确认根因，输出方案 |
| confirmation | S6 | 验证闭环 |
| escalation | S0_FAILED / human_escalation | 人工升级 |

这种相似不是偶然——两个项目解决的是同一类问题（结构化诊断），所以会自然演化出相近的状态流转。

### 3.4 相似性：都有知识检索机制

两个项目都认识到**单纯依赖 LLM 的幻觉推理是不可靠的**，必须用结构化知识来约束和引导：

**ops-agent：**
- 本地 JSONL 格式 SOP 文件
- 自定义 TF-IDF + identity anchoring 稀疏检索
- SOPQuerySubAgent 作为独立子智能体负责 SOP 导航
- 核心原则：知识内置，可移植，无外部依赖

**htp kb-service：**
- PostgreSQL 存储知识库条目（kbd_entry）+ SOP 文档
- 三轨路由：SOP优先 → KBD覆盖 → 人工兜底
- LLM 意图识别 → category_id → 路由查询
- 嵌入向量检索（z.ai embedding + bge-small 降级）
- 核心原则：知识外部化，可管理，支持热更新和 A/B 测试

---

## 四、差异性分析

### 4.1 核心差异：工具调用的「副作用」

这是两个项目最本质的区别，也是设计上最重要的分叉点。

**ops-agent 的工具——副作用极小：**

| 工具 | 副作用 |
|------|--------|
| `query_sop` | 纯读，无副作用 |
| `get_info_from_user` | 发起一次人机交互 |
| `request_approval` | 记录一次审批请求 |

ops-agent **不直接执行任何系统命令**。它的"行动"是向人类推荐操作步骤，执行权始终在人手里。这是一个有意为之的设计决策：让 AI 成为**向导**，而不是**操纵者**。

**htp 的工具——有真实副作用：**

| 工具 | 副作用 |
|------|--------|
| `acli_service_restart` | 重启 HCI 节点服务（risk_level=2） |
| `acli_netdoctor` | 执行网络诊断（risk_level=2） |
| `acli_network_nic_up` | 启用网卡（risk_level=2） |
| `get_active_alerts` | 查询告警（risk_level=1，只读） |
| `acli_system_top` | 读取系统负载（risk_level=1，只读） |

htp 通过 SSH（asyncssh）直接在 HCI 节点上执行命令。`risk_level=2` 的工具会先向用户推送 SSE 事件等待确认，超时 120 秒后自动取消。这是一个**自动化 + 人工监督**的混合模型。

**这个差异的深层含义：**

ops-agent 的定位是"经验丰富的专家助理"，它能给建议但不越权。htp 的定位是"半自动化运维平台"，它能直接采取行动但有风险管控。两个定位都合理，服务于不同的场景和用户群体。

### 4.2 差异：知识获取方式

| 维度 | ops-agent | htp |
|------|-----------|-----|
| **知识载体** | 本地 JSONL 文件 | PostgreSQL + 嵌入向量DB |
| **知识更新** | 重新生成 JSONL，重启服务 | 调 API 入库，实时生效 |
| **知识结构** | 自由格式（per-step 字段） | 结构化（title/content/category） |
| **实时数据** | 无（无法查询 HCI 平台） | 有（SCP REST API + acli SSH） |
| **SOP 路由** | TF-IDF 关键词 + PageIndex 导航 | LLM 意图识别 → category_id 查表 |
| **降级策略** | 无（SOP 未命中则机制推理） | human_escalation（第3轨） |

ops-agent 的知识是**内嵌的**：SOP 随程序打包部署，无需外部连接。这使它极具可移植性，可以在网络受限环境或开发调试场景中独立运行。

htp 的知识是**外化的**：kb-service 独立运行，支持知识的增删改查、版本管理（A/B 测试）、命中统计（`hits` 路由），形成了完整的知识管理闭环。

### 4.3 差异：对话持久化

**ops-agent：**
- 对话状态存于内存（`AgentExecution` 对象）
- 轨迹持久化到本地 JSONL 文件（用于离线分析）
- 进程终止则状态丢失
- 天然单用户

**htp：**
- 对话存于 PostgreSQL（conversation / message 表）
- 会话状态存于 Redis（Pod 分配、用户确认、ask_user 等待）
- 服务重启不丢对话
- 天然多用户（case_id / user_id 隔离）

### 4.4 差异：部署复杂度

**ops-agent：**
```bash
uv run ops-cli run                         # 开发/本地调试
python -m ops_agent.server.acp_server      # ACP JSON-RPC 服务
docker run ops-agent:latest                # Docker 单容器
```

**htp：**
```yaml
# deploy/helm/hci-platform/values.yaml
services:
  - api-gateway      # 流量入口、WebSocket
  - conversation-service  # 对话管理 + ReAct 执行
  - case-service          # 工单 CRUD
  - kb-service            # 知识库 + RAG
  - scheduler-service     # K8s Pod 热备池调度
  + PostgreSQL + Redis + OpenTelemetry + Grafana + Loki + Tempo
```

htp 在 K3s 上通过 Helm Chart 部署，涉及 5 个微服务 + 完整可观测性栈，开发环境用 Docker Compose。ops-agent 则是单二进制或单容器，无状态，极轻量。

### 4.5 差异：实时平台感知

这是 htp 相对于 ops-agent 最核心的能力优势：

htp 能在对话过程中**实时查询 HCI 平台状态**：
- `get_active_alerts`：读取 SCP 告警系统，获取当前活跃告警
- `get_failed_tasks`：读取失败任务列表
- `get_vm_list`：读取虚拟机清单
- `acli_system_top`：读取节点实时负载
- `acli_log_get`：读取系统日志

ops-agent 则是**完全无感知的**：它只能通过 `get_info_from_user` 询问用户"告警信息是什么"，然后等待用户手动输入。用户成为了数据搬运工。

这个差距的根本原因是：ops-agent 是离线引擎，不持有 HCI 平台的连接凭证；htp 是在线平台，通过 SCP adapter 和 acli adapter 维护着与 HCI 节点的连接。

---

## 五、集成关系：ops-agent 是如何被引入 htp 的

### 5.1 集成架构（feature/ops-agent-integration 分支）

htp 在 `feature/ops-agent-integration` 分支中实现了 ops-agent 的接入，方案是**松耦合的 AI 助手替换**：

```
              htp conversation-service
                    │
              AIAssistantRegistry
                    │
         ┌──────────┴──────────────┐
         │                         │
  OpenClawAssistant          OpsAgentAssistant   ← 新增（feature/ops-agent-integration）
         │                         │
   OpenClaw 内部            ops-agent ACP Server
   /v1/chat/completions     /v1/chat/completions   ← OpenAI-compatible API
```

用户在创建对话时通过 `assistant_type` 参数指定使用 `ops-agent` 助手：
```
POST /api/conversations/?case_id=Q20260428xxxxx&assistant_type=ops-agent
```

`OpsAgentAssistant` 继承了与 `OpenClawAssistant` 相同的接口（`AIAssistantClient` Protocol），但调用的是 ops-agent 暴露的 OpenAI-compatible 端点（ops-agent ACP Server / OpenAI-compatible mode），超时设置从 120s 扩展到 180s（因为 ops-agent 执行复杂推理链耗时较长）。

### 5.2 集成的技术实现细节

**ops-agent 侧（供体）：**

ops-agent 通过 `feature/openai-compatible-api` 分支暴露了 OpenAI-compatible API：
```
POST /v1/chat/completions   ← 标准 OpenAI 格式
Authorization: Bearer <token>
```

内部仍然运行完整的 ReAct + SOP 推理链，但输出以 SSE 流式返回，格式与 OpenAI chat completions 完全兼容。

**htp 侧（消费方）：**

`OpsAgentAssistant` 的实现：
- 接收来自 htp conversation-service 的 messages 列表
- 转发给 ops-agent 的 `/v1/chat/completions` 端点
- 以 SSE 流式透传 ops-agent 的输出
- 不使用 pod_endpoint（ops-agent 不支持 K8s Pod 热备池模式）

```python
class OpsAgentAssistant:
    def __init__(self, base_url: str, api_key: str | None = None, ...):
        self.base_url = base_url.rstrip("/")
        _read_timeout = float(os.environ.get("AI_CLIENT_READ_TIMEOUT_SEC", "180.0"))  # OA执行可能较慢
        ...
    
    async def chat_completion_stream(self, messages, user_id, pod_endpoint=None, model=""):
        url = f"{self.base_url}/v1/chat/completions"
        # pod_endpoint 被忽略（OA不支持热备池）
        ...
```

### 5.3 集成的设计权衡

这个集成方案有几个重要的权衡选择：

**选择松耦合 API 集成而非代码级集成：**

最简单的集成方式是直接 `import ops_agent` 在 htp 内运行。但团队选择了 HTTP API 边界，理由（从代码注释和文档推断）：
1. 独立部署，独立扩容——ops-agent 可以有自己的资源配额
2. 技术栈隔离——ops-agent 可以独立迭代，不影响 htp 主线
3. 故障隔离——ops-agent 崩溃不会拖垮 htp
4. 协议标准化——OpenAI-compatible API 是业界标准，未来可以替换其他引擎

**放弃了 htp 的部分能力：**

接入 ops-agent 后，`OpsAgentAssistant` **不使用** htp 的以下能力：
- `EnvironmentClient`（实时 HCI 数据）
- `KBClient`（kb-service 知识库）
- `SchedulerClient`（Pod 热备池）
- `ToolRouter`（acli / SCP 工具执行）
- `ConfirmService`（高危操作二次确认）

这意味着**在 ops-agent 模式下，htp 只是一个 UI 壳和对话记录层**，实际的推理和知识检索全部在 ops-agent 内部完成。这是一种"以新换旧"而非"融合增强"的接入方式——在初期降低集成风险，但长期来看无法充分利用 htp 平台的实时数据能力。

---

## 六、能力互补图谱

```
┌─────────────────────────────────────────────────────────┐
│                  HCI 排障能力全景                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ops-agent 强项              htp 强项                    │
│  ┌──────────────┐            ┌──────────────────┐       │
│  │ SOP 精准引导  │            │ 实时平台数据感知  │       │
│  │ 推理可解释性  │            │ 工单生命周期管理  │       │
│  │ 离线可运行    │            │ 团队协作支撑      │       │
│  │ 可移植、轻量  │            │ 知识库管理与运营  │       │
│  │ 500步深度推理 │            │ 多租户、审计追踪  │       │
│  └──────────────┘            │ K8s 弹性部署      │       │
│                              └──────────────────┘       │
│                                                         │
│  两者共有                                                │
│  ┌───────────────────────────────┐                      │
│  │ ReAct 驱动的诊断流程           │                      │
│  │ 声明式工具注册表               │                      │
│  │ 诊断阶段状态机（S0-S6）         │                      │
│  │ LLM + 知识库的混合推理模式      │                      │
│  └───────────────────────────────┘                      │
│                                                         │
│  当前集成方式（松耦合 API）                               │
│  ops-agent ──OpenAI-compatible API──▶ htp OpsAgentAssistant │
│  （只用 htp 的 UI 层，放弃了 htp 的数据感知能力）            │
└─────────────────────────────────────────────────────────┘
```

---

## 七、深层技术对比：知识检索引擎

两个项目在知识检索上的实现差异最能体现各自的设计哲学：

### 7.1 ops-agent：稀疏检索 + 子智能体导航

```
用户问题 → TF-IDF 关键词匹配 → 命中 SOP 文档
                                      │
                                SOPQuerySubAgent（子智能体）
                                      │
                                PageIndex 导航（按当前步骤定位章节）
                                      │
                              inject context → 主智能体继续推理
```

关键设计：`SOPQuerySubAgent` 作为独立的 Tool-as-SubAgent，拥有自己的推理能力，能在 SOP 文档内部导航，找到与当前诊断步骤最相关的章节注入上下文，而不是简单地返回整个 SOP 文件。

这解决了一个实际问题：SOP 文档通常很长（几十个步骤），把整份文档塞进上下文既浪费 token 又降低信噪比。子智能体导航让主智能体每次只看到最相关的那段。

### 7.2 htp：三轨路由 + 向量检索

```
用户问题 → classify/intent（LLM 意图识别）→ category_id
                                                    │
                              ┌─────────────────────┤
                              ▼                     ▼
                    SOP 轨（尚未完整）        KBD 轨（BM25 全文）
                              │                     │
                        命中返回 SOP          命中返回历史案例
                              │                     │
                              └─────────┬───────────┘
                                        ▼
                             无命中 → human_escalation
```

关键设计：通过 LLM 做意图识别，把自然语言问题映射到结构化的 category_id（198 个分类节点），然后用 category_id 做精确匹配，比纯关键词检索更健壮。但这引入了额外的 LLM 调用开销（每次检索需要一次意图识别调用）。

**值得注意的是：** kb-service 中还保留了 `SopMatcher`（关键字精确路由），这与 ops-agent 的 TF-IDF 检索在思路上非常接近，说明 htp 也走过类似的路。`knowledge_retriever.py` 的注释明确写到"废弃 kb_sop_node 关键字路由（'已知最差的触发机制'）"——这是 htp 踩过坑后的架构演化记录。

---

## 八、未来演进的关键问题

从第一性原理出发，两个项目在关系上面临几个根本性的设计问题：

### 8.1 ops-agent 的实时数据盲区

ops-agent 当前的局限：遇到"告警状态是什么"这类问题，只能通过 `get_info_from_user` 让用户手动粘贴数据。

**可行的补强路径：**
- ops-agent 添加"环境探针"工具（接入 SCP API / acli SSH）
- htp 在调用 ops-agent 前先预取实时数据，通过 messages 系统消息注入
- 后者风险更低（不改变 ops-agent 的离线设计原则）

### 8.2 htp 的推理深度天花板

htp 的 MAX_STEPS=15 在复杂故障场景下可能过早截断。ops-agent 的 500 步设计在这里有明显优势。但直接提高步数会增加 LLM 调用成本和用户等待时间。

**可行的补强路径：**
- 动态步数：根据故障复杂度（S0 识别结果）调整上限
- 引入 ops-agent 处理深度推理：htp 先做 S0/S1 快速定位，复杂案例转发给 ops-agent 做深度推理

### 8.3 知识孤岛问题

当前两个项目的知识库相互独立：
- ops-agent 有自己的 SOP JSONL 文件
- htp 有自己的 kb-service（kbd_entry + sop_document）

两套 SOP 的内容是否一致？如何保持同步？这是实际运维中会遇到的问题。

**可行的统一路径：**
- ops-agent 从 htp kb-service 拉取 SOP（ops-agent 成为纯引擎，知识从 htp 获取）
- htp 使用 ops-agent 的检索引擎（ops-agent 的 TF-IDF + 子智能体导航成为 htp 的检索能力）
- 两者共享 SOP JSONL 格式（统一数据标准）

---

## 九、总结

### 相关性（为什么是一家人）

1. **共同的领域知识**：都深度编码了 HCI 平台排障的领域知识（S0-S6 流程、acli 工具、SCP API 等）
2. **共同的技术路线**：都选择了 ReAct 循环 + 声明式工具注册 + 阶段状态机
3. **明确的集成意图**：`feature/ops-agent-integration` 分支的存在说明两个项目是有意识地协同演进的
4. **互补的能力缺口**：ops-agent 缺实时数据感知，htp 缺深度推理能力，两者的缺口恰好是对方的强项

### 差异性（为什么各自独立存在）

1. **使用场景不同**：ops-agent 适合单人、离线、深度排查；htp 适合团队、在线、快速响应
2. **副作用控制不同**：ops-agent 不执行系统命令（最小侵入）；htp 可直接执行命令（需风险管控）
3. **部署复杂度不同**：ops-agent 是轻量单二进制；htp 是完整的微服务平台
4. **知识管理不同**：ops-agent 知识内嵌静态；htp 知识外化可运营

### 最优的未来关系形态

基于以上分析，两个项目最优的协作模式应该是**分层互补**，而非当前的"替换"模式：

```
htp（平台层）
  ├── 实时数据感知（SCP API + acli SSH）
  ├── 工单管理、审计、多租户
  └── 知识运营（kb-service）
       │
       │ 调用（深度推理场景）
       ▼
ops-agent（引擎层）
  ├── 深度 SOP 推理（500步上限）
  ├── 知识检索与注入
  └── 可解释推理轨迹
       │
       │ 接收实时数据（由 htp 预取后注入）
       └── 实时上下文补强
```

这种模式下，htp 负责"连接世界"（获取实时数据、管理知识库），ops-agent 负责"深度思考"（SOP 引导的可解释推理），两者通过标准化的 OpenAI-compatible API 协作，各自聚焦于自己最擅长的部分。

---

*文档生成于 ops-agent worktree `feature/ops-agent-docs-main-20260428` 与 htp worktree `feature/htp-analysis-main-20260428` 的代码分析，基于两个项目 main 分支（截至 2026-04-28）及 `feature/ops-agent-integration` 分支的实现。*
