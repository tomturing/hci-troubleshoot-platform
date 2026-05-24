# 故障排查 Agent 设计方案：Graph + ReAct 组合模式

## 一、需求背景

### 1.1 故障排查工作流

故障排查是一个典型的多步骤、需要迭代验证的复杂工作流程：

```
意图识别 → 故障定位 → 假设生成 → 验证执行 → 根因确认 → 方案输出 → 验证闭环
```

### 1.2 流程特点分析

| 流程阶段 | 特点 | 模式需求 |
|---------|------|---------|
| 意图识别 | 固定步骤，结构化输出 | Graph Node |
| 故障定位 | 固定步骤，需要调用工具收集信息 | Graph Node + Tools |
| 假设生成 → 验证 → 确认 | 需要迭代验证，可能多轮循环 | ReAct 循环 |
| 方案输出 | 固定步骤，结构化输出 | Graph Node |
| 验证闭环 | 需要人工确认方案有效性 | Human-in-the-loop |

### 1.3 核心挑战

1. **步骤顺序性**：流程有明确的先后顺序
2. **迭代验证**：假设验证可能需要多轮 Thought → Action → Observation
3. **分支决策**：不同假设走不同验证路径
4. **状态保持**：需要跟踪当前假设、验证结果等中间状态
5. **人工介入**：关键决策点需要人工审批

---

## 二、设计方案：Graph + ReAct 组合

### 2.1 设计理念

采用 **Graph-based 为主骨架 + ReAct 为循环内核** 的组合设计：

- **Graph-based Control Flow**：管理整体流程编排、状态管理、步骤流转
- **ReAct 模式**：嵌入假设验证阶段，实现 Thought → Action → Observation 循环
- **Human-in-the-loop**：关键节点加入人工审批机制

### 2.2 整体架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Graph-based 故障排查 Agent                               │
│                                                                             │
│   ┌─────────────────┐                                                       │
│   │  IntentNode     │  意图识别                                              │
│   │  (结构化输出)    │                                                       │
│   └────────┬────────┘                                                       │
│            │                                                                │
│            ▼                                                                │
│   ┌─────────────────┐                                                       │
│   │  LocateNode     │  故障定位                                              │
│   │  (调用收集工具)  │                                                       │
│   └────────┬────────┘                                                       │
│            │                                                                │
│            ▼                                                                │
│   ┌─────────────────────────────────────────────────────────────────┐      │
│   │                    HypothesisLoopNode                            │      │
│   │                                                                  │      │
│   │   ┌───────────────────────────────────────────────────────────┐ │      │
│   │   │                  内部 ReAct 循环                           │ │      │
│   │   │                                                           │ │      │
│   │   │  ┌─────────┐    ┌─────────┐    ┌─────────┐               │ │      │
│   │   │  │ Thought │───►│ Action  │───►│Observ.  │───► 循环     │ │      │
│   │   │  │ (假设)  │    │ (验证)  │    │ (结果)  │    或结束     │ │      │
│   │   │  └─────────┘    └─────────┘    └─────────┘               │ │      │
│   │   │                                                           │ │      │
│   │   │  工具: check_logs, query_metrics, test_connectivity...   │ │      │
│   │   │                                                           │ │      │
│   │   └───────────────────────────────────────────────────────────┘ │      │
│   │                                                                  │      │
│   │  输出: RootCause | NeedMoreInfo                                │      │
│   │                                                                  │      │
│   └────────────────────────────────┬────────────────────────────────┘      │
│                                     │                                       │
│                     ┌───────────────┴───────────────┐                      │
│                     │                               │                      │
│                     ▼                               ▼                      │
│           ┌─────────────────┐              ┌─────────────────┐            │
│           │  SolutionNode   │              │  BackToLocate   │            │
│           │  (生成方案)      │              │  (补充信息)     │            │
│           └────────┬────────┘              └────────┬────────┘            │
│                    │                                │                      │
│                    ▼                                │                      │
│           ┌─────────────────┐                       │                      │
│           │  ValidateNode   │  验证闭环             │                      │
│           │  (人工确认)      │◄─────────────────────┤                      │
│           └────────┬────────┘                       │                      │
│                    │                                │                      │
│                    ▼                                │                      │
│           ┌─────────────────┐                       │                      │
│           │      End        │                       │                      │
│           │  (FinalResult)  │                       │                      │
│           └─────────────────┘                       │                      │
│                                                     │                      │
│                     循环直到根因确认或资源耗尽         │                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 模式职责分工

| 模式 | 负责范围 | 具体职责 |
|------|---------|---------|
| **Graph-based** | 整体骨架 | 步骤编排、状态管理、流转控制、可视化、断点恢复 |
| **ReAct** | 假设验证阶段 | Thought(生成假设) → Action(调用验证工具) → Observation(分析结果) 循环 |
| **Human-in-the-loop** | 关键决策点 | 根因确认审批、方案执行审批、敏感操作审批 |

---

## 三、详细设计

### 3.1 状态定义

```python
from dataclasses import dataclass, field
from typing import Literal, Any
from datetime import datetime


@dataclass
class TroubleshootState:
    """故障排查状态 - 跨节点持久化"""
    
    # ===== 输入 =====
    user_query: str                           # 用户原始查询
    conversation_id: str                      # 会话 ID
    created_at: datetime = field(default_factory=datetime.now)
    
    # ===== 意图识别结果 =====
    intent: Literal[
        'performance',    # 性能问题
        'availability',   # 可用性问题
        'security',       # 安全问题
        'configuration',  # 配置问题
        'unknown'         # 未知类型
    ] | None = None
    
    problem_type: str | None = None           # 问题子类型
    severity: Literal['high', 'medium', 'low'] | None = None  # 严重程度
    affected_component: str | None = None     # 受影响组件
    
    # ===== 故障定位结果 =====
    collected_info: dict[str, Any] = field(default_factory=dict)
    # 结构示例:
    # {
    #     'pod_status': {...},
    #     'events': [...],
    #     'logs': [...],
    #     'metrics': {...},
    #     'config': {...},
    # }
    
    # ===== 假设验证循环 =====
    hypotheses: list[Hypothesis] = field(default_factory=list)      # 所有生成的假设
    current_hypothesis: Hypothesis | None = None                   # 当前验证的假设
    verified_hypotheses: list[VerifiedHypothesis] = field(default_factory=list)  # 已验证结果
    
    retry_count: int = 0                      # 循环次数
    max_retries: int = 5                      # 最大循环次数
    
    # ===== 根因 =====
    root_cause: str | None = None             # 确认的根因
    root_cause_type: str | None = None        # 根因类型分类
    evidence: list[str] = field(default_factory=list)  # 支持证据
    
    # ===== 方案 =====
    solution: Solution | None = None          # 解决方案
    solution_steps: list[str] = field(default_factory=list)  # 方案步骤
    validated: bool = False                   # 是否验证有效
    
    # ===== 执行状态 =====
    current_step: str = 'intent'              # 当前步骤
    completed_steps: list[str] = field(default_factory=list)  # 已完成步骤
    error: str | None = None                  # 错误信息
    
    # ===== 使用统计 =====
    total_tokens: int = 0
    total_requests: int = 0
    tool_calls: int = 0


@dataclass
class Hypothesis:
    """假设结构"""
    id: str                                   # 假设 ID
    description: str                          # 假设描述
    probability: float                        # 可能性 (0-1)
    verification_tools: list[str]             # 建议的验证工具
    generated_at: datetime
    
    # 假设来源
    source: Literal['model', 'knowledge_base', 'heuristic'] = 'model'


@dataclass
class VerifiedHypothesis:
    """已验证假设"""
    hypothesis: Hypothesis
    result: Literal['confirmed', 'rejected', 'partial', 'uncertain']
    evidence: list[str]
    verification_details: str
    verified_at: datetime


@dataclass
class Solution:
    """解决方案结构"""
    description: str                          # 方案描述
    steps: list[SolutionStep]                 # 具体步骤
    risk_level: Literal['low', 'medium', 'high']  # 执行风险
    requires_approval: bool                   # 是否需要审批
    estimated_time: str                       # 预估时间
    
    # 相关根因
    root_cause_id: str | None = None


@dataclass
class SolutionStep:
    """方案步骤"""
    order: int
    action: str                               # 操作描述
    command: str | None = None                # 具体命令 (如有)
    verification: str                         # 如何验证成功
    rollback: str | None = None               # 回滚方法
```

### 3.2 依赖定义

```python
@dataclass
class TroubleshootDeps:
    """故障排查依赖 - 不可变配置"""
    
    # ===== Agent 配置 =====
    intent_agent: Agent                       # 意图识别 Agent
    locate_agent: Agent                       # 故障定位 Agent
    hypothesis_agent: Agent                   # 假设生成 Agent (ReAct)
    solution_agent: Agent                     # 方案生成 Agent
    
    # ===== 客户端 =====
    k8s_client: KubernetesClient              # Kubernetes 客户端
    log_client: LogClient                     # 日志查询客户端
    metrics_client: MetricsClient             # 指标查询客户端
    config_client: ConfigClient               # 配置查询客户端
    
    # ===== 知识库 =====
    knowledge_base: KnowledgeBase             # 故障知识库
    historical_cases: HistoricalCases         # 历史案例库
    
    # ===== 配置 =====
    max_hypothesis_retries: int = 5           # 最大假设验证次数
    require_approval_for: list[str] = field(
        default_factory=lambda: ['restart_pod', 'delete_resource', 'modify_config']
    )                                         # 需审批的操作
    
    # ===== 可观测性 =====
    tracer: Tracer                            # OTel Tracer
    logfire_enabled: bool = True
```

### 3.3 Graph 节点设计

#### 3.3.1 意图识别节点 (IntentNode)

```python
from pydantic import BaseModel
from pydantic_graph import BaseNode, GraphRunContext
from typing_extensions import Self


class IntentOutput(BaseModel):
    """意图识别输出"""
    type: Literal['performance', 'availability', 'security', 'configuration', 'unknown']
    severity: Literal['high', 'medium', 'low']
    affected_component: str | None
    confidence: float
    suggested_tools: list[str]


@dataclass
class IntentNode(BaseNode[TroubleshootState, TroubleshootDeps, FinalResult]):
    """意图识别节点
    
    职责:
    - 分析用户查询，识别故障类型
    - 判断严重程度
    - 确定受影响组件
    - 建议信息收集工具
    
    输入: user_query (from state)
    输出: IntentOutput → 更新 state.intent, state.severity 等
    下游: LocateNode
    """
    
    async def run(
        self, 
        ctx: GraphRunContext[TroubleshootState, TroubleshootDeps]
    ) -> LocateNode:
        with logfire.span('intent_recognition', query=ctx.state.user_query):
            # 调用意图识别 Agent
            result = await ctx.deps.intent_agent.run(
                ctx.state.user_query,
                output_type=IntentOutput,
            )
            
            # 更新状态
            ctx.state.intent = result.output.type
            ctx.state.severity = result.output.severity
            ctx.state.problem_type = result.output.type
            ctx.state.affected_component = result.output.affected_component
            
            # 记录完成步骤
            ctx.state.completed_steps.append('intent')
            ctx.state.current_step = 'locate'
            
            logfire.info(
                'intent recognized',
                type=result.output.type,
                severity=result.output.severity,
                component=result.output.affected_component,
            )
            
            return LocateNode(suggested_tools=result.output.suggested_tools)
```

#### 3.3.2 故障定位节点 (LocateNode)

```python
@dataclass
class LocateNode(BaseNode[TroubleshootState, TroubleshootDeps, FinalResult]):
    """故障定位节点
    
    职责:
    - 根据意图调用相应的信息收集工具
    - 获取 Pod 状态、事件、日志、指标等
    - 初步过滤无关信息
    - 构建故障上下文
    
    输入: intent, affected_component (from state)
    输出: collected_info → 更新 state.collected_info
    下游: HypothesisLoopNode
    """
    
    suggested_tools: list[str] = field(default_factory=list)
    
    async def run(
        self,
        ctx: GraphRunContext[TroubleshootState, TroubleshootDeps]
    ) -> HypothesisLoopNode:
        with logfire.span('fault_location', intent=ctx.state.intent):
            collected = {}
            
            # 根据意图类型收集不同信息
            if ctx.state.intent == 'availability':
                collected.update(await self._collect_availability_info(ctx))
            elif ctx.state.intent == 'performance':
                collected.update(await self._collect_performance_info(ctx))
            elif ctx.state.intent == 'configuration':
                collected.update(await self._collect_config_info(ctx))
            elif ctx.state.intent == 'security':
                collected.update(await self._collect_security_info(ctx))
            else:
                # unknown 类型，收集所有基本信息
                collected.update(await self._collect_basic_info(ctx))
            
            # 更新状态
            ctx.state.collected_info = collected
            ctx.state.completed_steps.append('locate')
            ctx.state.current_step = 'hypothesis'
            
            logfire.info('collected_info', keys=list(collected.keys()))
            
            return HypothesisLoopNode()
    
    async def _collect_availability_info(self, ctx) -> dict:
        """收集可用性问题相关信息"""
        component = ctx.state.affected_component
        
        return {
            'pod_status': await ctx.deps.k8s_client.get_pod_status(component),
            'pod_events': await ctx.deps.k8s_client.get_events(component, limit=50),
            'recent_logs': await ctx.deps.log_client.query(
                component, 
                time_range='1h',
                level=['error', 'warn'],
            ),
            'service_status': await ctx.deps.k8s_client.get_service_status(component),
            'endpoint_health': await ctx.deps.k8s_client.check_endpoint(component),
        }
    
    async def _collect_performance_info(self, ctx) -> dict:
        """收集性能问题相关信息"""
        component = ctx.state.affected_component
        
        return {
            'resource_metrics': await ctx.deps.metrics_client.query_range(
                component,
                metrics=['cpu', 'memory', 'network_io', 'disk_io'],
                time_range='1h',
            ),
            'latency_metrics': await ctx.deps.metrics_client.query_range(
                component,
                metrics=['request_latency', 'error_rate'],
                time_range='1h',
            ),
            'slow_logs': await ctx.deps.log_client.query(
                component,
                time_range='1h',
                keywords=['slow', 'timeout', 'latency'],
            ),
        }
    
    async def _collect_config_info(self, ctx) -> dict:
        """收集配置问题相关信息"""
        component = ctx.state.affected_component
        
        return {
            'current_config': await ctx.deps.config_client.get_config(component),
            'config_history': await ctx.deps.config_client.get_history(component, limit=10),
            'config_diff': await ctx.deps.config_client.compare_with_baseline(component),
        }
    
    async def _collect_basic_info(self, ctx) -> dict:
        """收集基本信息"""
        return await self._collect_availability_info(ctx)
```

#### 3.3.3 假设验证循环节点 (HypothesisLoopNode) - **核心 ReAct 实现**

```python
@dataclass
class HypothesisLoopNode(BaseNode[TroubleshootState, TroubleshootDeps, FinalResult]):
    """假设验证循环节点 - 内嵌 ReAct 模式
    
    职责:
    - 基于收集信息生成故障假设
    - 调用验证工具检验假设
    - 分析验证结果，迭代生成新假设
    - 直到确认根因或资源耗尽
    
    内部流程 (ReAct):
    1. Thought: 分析信息，生成假设
    2. Action: 调用验证工具
    3. Observation: 分析验证结果
    4. Decision: 根因确认 or 继续循环
    
    输入: collected_info (from state)
    输出: root_cause | NeedMoreInfo
    下游: SolutionNode | BackToLocateNode
    """
    
    async def run(
        self,
        ctx: GraphRunContext[TroubleshootState, TroubleshootDeps]
    ) -> SolutionNode | BackToLocateNode | End[FinalResult]:
        with logfire.span('hypothesis_loop', retry=ctx.state.retry_count):
            
            # 检查是否超过最大重试
            if ctx.state.retry_count >= ctx.state.max_retries:
                logfire.warning('max_retries_exceeded')
                ctx.state.root_cause = "无法确定根因，需要人工介入"
                return SolutionNode()
            
            # ===== ReAct 循环 =====
            # Thought: 生成假设
            hypothesis = await self._generate_hypothesis(ctx)
            ctx.state.current_hypothesis = hypothesis
            ctx.state.hypotheses.append(hypothesis)
            
            logfire.info('hypothesis_generated', hypothesis=hypothesis.description)
            
            # Action: 验证假设
            verification_result = await self._verify_hypothesis(ctx, hypothesis)
            
            # Observation: 分析结果
            analysis = await self._analyze_verification(ctx, hypothesis, verification_result)
            
            # Decision: 判断下一步
            if analysis.root_cause_found:
                ctx.state.root_cause = analysis.root_cause
                ctx.state.root_cause_type = analysis.root_cause_type
                ctx.state.evidence = analysis.evidence
                ctx.state.completed_steps.append('hypothesis')
                ctx.state.current_step = 'solution'
                
                logfire.info('root_cause_found', root_cause=analysis.root_cause)
                return SolutionNode()
            
            elif analysis.need_more_info:
                logfire.info('need_more_info', missing=analysis.missing_info)
                return BackToLocateNode(missing_info=analysis.missing_info)
            
            else:
                # 继续循环
                ctx.state.retry_count += 1
                ctx.state.verified_hypotheses.append(
                    VerifiedHypothesis(
                        hypothesis=hypothesis,
                        result=analysis.result,
                        evidence=analysis.evidence,
                        verification_details=analysis.details,
                    )
                )
                return HypothesisLoopNode()
    
    async def _generate_hypothesis(self, ctx) -> Hypothesis:
        """生成故障假设 (Thought)"""
        
        # 构建假设生成提示
        prompt = self._build_hypothesis_prompt(ctx)
        
        # 调用假设生成 Agent
        result = await ctx.deps.hypothesis_agent.run(
            prompt,
            output_type=Hypothesis,
        )
        
        return result.output
    
    def _build_hypothesis_prompt(self, ctx) -> str:
        """构建假设生成提示"""
        verified_summary = '\n'.join([
            f"- {v.hypothesis.description}: {v.result}"
            for v in ctx.state.verified_hypotheses
        ]) if ctx.state.verified_hypotheses else "无已验证假设"
        
        return f'''
你是一个故障排查专家，请基于以下信息生成故障假设。

## 用户问题
{ctx.state.user_query}

## 已收集信息
意图类型: {ctx.state.intent}
受影响组件: {ctx.state.affected_component}
严重程度: {ctx.state.severity}

收集详情:
{json.dumps(ctx.state.collected_info, indent=2, ensure_ascii=False)}

## 已验证假设
{verified_summary}

## 任务
1. 分析收集的信息
2. 生成一个最可能的故障假设
3. 指出建议的验证工具和方法

## 输出要求
- 描述假设时要具体，不要模糊
- 指出验证工具时要明确工具名称
- 给出概率评估 (0-1)
'''
    
    async def _verify_hypothesis(self, ctx, hypothesis: Hypothesis) -> dict:
        """验证假设 (Action + Observation)"""
        
        # 使用 ReAct Agent 执行验证
        react_prompt = f'''
请验证以下假设: {hypothesis.description}

可用工具: {hypothesis.verification_tools}

请调用相关工具获取验证信息，并分析结果。
如果确认找到根因，返回 "ROOT_CAUSE_FOUND: <根因描述>"
如果需要更多信息，返回 "NEED_MORE_INFO: <需要的信息>"
'''
        
        async with ctx.deps.hypothesis_agent.iter(react_prompt) as run:
            tool_results = []
            
            async for node in run:
                if isinstance(node, CallToolsNode):
                    # 工具执行
                    for tool_call in node.model_response.tool_calls:
                        tool_name = tool_call.tool_name
                        tool_args = tool_call.args
                        
                        with logfire.span('tool_call', tool=tool_name):
                            result = await self._execute_verification_tool(
                                ctx, tool_name, tool_args
                            )
                            tool_results.append({
                                'tool': tool_name,
                                'args': tool_args,
                                'result': result,
                            })
                            
                            ctx.state.tool_calls += 1
        
        return {
            'hypothesis_id': hypothesis.id,
            'tool_results': tool_results,
            'model_response': run.result.output if run.result else None,
        }
    
    async def _execute_verification_tool(
        self, 
        ctx, 
        tool_name: str, 
        tool_args: dict
    ) -> str:
        """执行验证工具"""
        
        # 工具映射
        tools = {
            'check_logs': lambda: ctx.deps.log_client.query(
                ctx.state.affected_component,
                keywords=tool_args.get('keywords', []),
                time_range=tool_args.get('time_range', '1h'),
            ),
            'query_metrics': lambda: ctx.deps.metrics_client.query_range(
                ctx.state.affected_component,
                metrics=tool_args.get('metrics', []),
                time_range=tool_args.get('time_range', '1h'),
            ),
            'test_connectivity': lambda: ctx.deps.k8s_client.test_connectivity(
                ctx.state.affected_component,
                target=tool_args.get('target'),
            ),
            'check_config': lambda: ctx.deps.config_client.get_config(
                ctx.state.affected_component,
            ),
            'describe_pod': lambda: ctx.deps.k8s_client.describe_pod(
                tool_args.get('pod_name', ctx.state.affected_component),
            ),
            'get_events': lambda: ctx.deps.k8s_client.get_events(
                ctx.state.affected_component,
                types=tool_args.get('types', []),
            ),
        }
        
        if tool_name in tools:
            return await tools[tool_name]()
        else:
            return f"未知工具: {tool_name}"
    
    async def _analyze_verification(
        self, 
        ctx, 
        hypothesis: Hypothesis,
        verification: dict
    ) -> AnalysisResult:
        """分析验证结果"""
        
        # 调用分析 Agent
        analysis_prompt = f'''
请分析以下假设验证结果:

假设: {hypothesis.description}

验证工具结果:
{json.dumps(verification['tool_results'], indent=2, ensure_ascii=False)}

请判断:
1. 假设是否被确认? (confirmed/rejected/partial/uncertain)
2. 是否找到了根因? 如果是，描述根因
3. 是否需要更多信息? 如果是，描述缺少什么
'''
        
        result = await ctx.deps.hypothesis_agent.run(
            analysis_prompt,
            output_type=AnalysisResult,
        )
        
        return result.output


@dataclass
class AnalysisResult:
    """验证分析结果"""
    result: Literal['confirmed', 'rejected', 'partial', 'uncertain']
    root_cause_found: bool
    root_cause: str | None
    root_cause_type: str | None
    evidence: list[str]
    need_more_info: bool
    missing_info: list[str] | None
    details: str
```

#### 3.3.4 方案生成节点 (SolutionNode)

```python
@dataclass
class SolutionNode(BaseNode[TroubleshootState, TroubleshootDeps, FinalResult]):
    """方案生成节点
    
    职责:
    - 根据根因生成解决方案
    - 评估方案风险
    - 分解为具体执行步骤
    - 标记需要审批的操作
    
    输入: root_cause (from state)
    输出: solution → 更新 state.solution
    下游: ValidateNode
    """
    
    async def run(
        self,
        ctx: GraphRunContext[TroubleshootState, TroubleshootDeps]
    ) -> ValidateNode:
        with logfire.span('solution_generation', root_cause=ctx.state.root_cause):
            
            # 查询历史类似案例
            similar_cases = await ctx.deps.historical_cases.find_similar(
                ctx.state.root_cause_type,
                limit=5,
            )
            
            # 生成方案
            prompt = f'''
请根据以下根因生成解决方案:

## 根因
{ctx.state.root_cause}

## 受影响组件
{ctx.state.affected_component}

## 问题类型
{ctx.state.intent}

## 收集的证据
{ctx.state.evidence}

## 类似历史案例
{json.dumps(similar_cases, indent=2, ensure_ascii=False)}

## 要求
1. 方案要具体可执行
2. 评估风险等级
3. 标记需要审批的敏感操作
4. 提供验证方法和回滚方案
'''
            
            result = await ctx.deps.solution_agent.run(
                prompt,
                output_type=Solution,
            )
            
            # 检查是否需要审批
            for step in result.output.steps:
                if any(
                    op in step.action.lower() 
                    for op in ctx.deps.require_approval_for
                ):
                    step.requires_approval = True
            
            # 更新状态
            ctx.state.solution = result.output
            ctx.state.solution_steps = [s.action for s in result.output.steps]
            ctx.state.completed_steps.append('solution')
            ctx.state.current_step = 'validate'
            
            logfire.info('solution_generated', steps=len(result.output.steps))
            
            return ValidateNode()
```

#### 3.3.5 验证闭环节点 (ValidateNode)

```python
@dataclass
class ValidateNode(BaseNode[TroubleshootState, TroubleshootDeps, FinalResult]):
    """验证闭环节点
    
    职责:
    - 人工确认方案有效性
    - 自动验证 (如果适用)
    - 记录验证结果
    
    输入: solution (from state)
    输出: validated → 更新 state.validated
    下游: End
    """
    
    async def run(
        self,
        ctx: GraphRunContext[TroubleshootState, TroubleshootDeps]
    ) -> End[FinalResult]:
        with logfire.span('validation'):
            
            # 检查方案中是否有需要审批的操作
            approval_required = any(
                step.requires_approval 
                for step in ctx.state.solution.steps
            )
            
            if approval_required:
                # 返回待审批结果，等待人工介入
                # 实际实现中，这里会触发审批流程
                # 然后通过 DeferredToolResults 恢复
                logfire.info('approval_required')
                # 此处简化为直接通过
                pass
            
            # 自动验证 (可选)
            auto_validation_result = await self._auto_validate(ctx)
            
            if auto_validation_result.success:
                ctx.state.validated = True
                ctx.state.completed_steps.append('validate')
            else:
                ctx.state.validated = False
                ctx.state.error = auto_validation_result.error
            
            logfire.info('validation_result', validated=ctx.state.validated)
            
            return End(FinalResult(
                root_cause=ctx.state.root_cause,
                root_cause_type=ctx.state.root_cause_type,
                evidence=ctx.state.evidence,
                solution=ctx.state.solution,
                validated=ctx.state.validated,
                verified_hypotheses=ctx.state.verified_hypotheses,
                completed_steps=ctx.state.completed_steps,
                total_tokens=ctx.state.total_tokens,
                tool_calls=ctx.state.tool_calls,
            ))
    
    async def _auto_validate(self, ctx) -> ValidationResult:
        """自动验证方案有效性"""
        
        # 基于方案类型的验证逻辑
        # 例如: 配置修复 → 检查配置是否正确
        # 例如: 重启 Pod → 检查 Pod 是否正常运行
        
        if 'restart' in ctx.state.solution.description.lower():
            # 检查 Pod 是否重启成功
            pod_status = await ctx.deps.k8s_client.get_pod_status(
                ctx.state.affected_component
            )
            if pod_status['status'] == 'Running':
                return ValidationResult(success=True)
            else:
                return ValidationResult(
                    success=False, 
                    error=f"Pod 状态异常: {pod_status['status']}"
                )
        
        # 默认返回成功 (依赖人工验证)
        return ValidationResult(success=True)


@dataclass
class ValidationResult:
    """验证结果"""
    success: bool
    error: str | None = None
```

#### 3.3.6 回退节点 (BackToLocateNode)

```python
@dataclass
class BackToLocateNode(BaseNode[TroubleshootState, TroubleshootDeps, FinalResult]):
    """回退到定位节点
    
    职责:
    - 补充收集缺失的信息
    - 更新收集信息状态
    - 重新进入假设循环
    
    输入: missing_info
    输出: 更新 collected_info
    下游: HypothesisLoopNode
    """
    
    missing_info: list[str] = field(default_factory=list)
    
    async def run(
        self,
        ctx: GraphRunContext[TroubleshootState, TroubleshootDeps]
    ) -> HypothesisLoopNode:
        with logfire.span('back_to_locate', missing=self.missing_info):
            
            # 补充收集缺失信息
            for info_type in self.missing_info:
                if info_type == 'pod_logs':
                    logs = await ctx.deps.log_client.query(
                        ctx.state.affected_component,
                        time_range='6h',
                        level=['error', 'warn', 'info'],
                    )
                    ctx.state.collected_info['pod_logs'] = logs
                
                elif info_type == 'pod_events':
                    events = await ctx.deps.k8s_client.get_events(
                        ctx.state.affected_component,
                        limit=100,
                    )
                    ctx.state.collected_info['pod_events'] = events
                
                elif info_type == 'resource_metrics':
                    metrics = await ctx.deps.metrics_client.query_range(
                        ctx.state.affected_component,
                        metrics=['cpu', 'memory'],
                        time_range='6h',
                    )
                    ctx.state.collected_info['resource_metrics'] = metrics
            
            logfire.info('info_collected', types=self.missing_info)
            
            return HypothesisLoopNode()
```

### 3.4 最终结果定义

```python
@dataclass
class FinalResult:
    """故障排查最终结果"""
    
    # ===== 根因 =====
    root_cause: str                           # 根因描述
    root_cause_type: str | None               # 根因分类
    evidence: list[str]                       # 支持证据
    
    # ===== 方案 =====
    solution: Solution | None                 # 解决方案
    
    # ===== 验证 =====
    validated: bool                           # 是否验证有效
    
    # ===== 过程记录 =====
    verified_hypotheses: list[VerifiedHypothesis]  # 所有验证过的假设
    completed_steps: list[str]                # 完成的步骤
    
    # ===== 统计 =====
    total_tokens: int                         # Token 使用量
    tool_calls: int                           # 工具调用次数
    
    # ===== 建议 =====
    suggestions: list[str] = field(default_factory=list)  # 额外建议
    
    def to_report(self) -> str:
        """生成排查报告"""
        return f'''
# 故障排查报告

## 问题概述
- 根因: {self.root_cause}
- 类型: {self.root_cause_type}
- 证据: {self.evidence}

## 解决方案
{self.solution.description if self.solution else "无"}

### 执行步骤
{chr(10).join(f"{i+1}. {s.action}" for i, s in enumerate(self.solution.steps)) if self.solution else ""}

## 排查过程
- 假设数量: {len(self.verified_hypotheses)}
- 工具调用: {self.tool_calls} 次
- Token 使用: {self.total_tokens}

## 验证状态
{self.validated and "已验证有效" or "待人工验证"}
'''
```

### 3.5 Graph 构建与运行

```python
from pydantic_graph import GraphBuilder


def build_troubleshoot_graph():
    """构建故障排查 Graph"""
    
    return GraphBuilder(
        nodes=[
            IntentNode,
            LocateNode,
            HypothesisLoopNode,
            SolutionNode,
            ValidateNode,
            BackToLocateNode,
        ],
    ).build()


async def troubleshoot(
    user_query: str,
    deps: TroubleshootDeps,
    conversation_id: str | None = None,
) -> FinalResult:
    """运行故障排查
    
    Args:
        user_query: 用户故障描述
        deps: 依赖配置
        conversation_id: 会话 ID (可选)
    
    Returns:
        FinalResult: 排查结果
    """
    # 初始化状态
    state = TroubleshootState(
        user_query=user_query,
        conversation_id=conversation_id or str(uuid7()),
    )
    
    # 构建 Graph
    graph = build_troubleshoot_graph()
    
    # 运行
    result = await graph.run(
        IntentNode(),
        state=state,
        deps=deps,
    )
    
    return result.output


# === 可视化 Graph ===

def visualize_graph():
    """生成 Graph 可视化"""
    graph = build_troubleshoot_graph()
    
    # 生成 Mermaid 图
    mermaid = graph.mermaid_diagram()
    print(mermaid)
    
    # 输出:
    # graph TD
    #   IntentNode --> LocateNode
    #   LocateNode --> HypothesisLoopNode
    #   HypothesisLoopNode --> SolutionNode
    #   HypothesisLoopNode --> BackToLocateNode
    #   HypothesisLoopNode --> End
    #   BackToLocateNode --> HypothesisLoopNode
    #   SolutionNode --> ValidateNode
    #   ValidateNode --> End
```

---

## 四、工具定义

### 4.1 验证工具集

```python
from pydantic_ai import Agent, RunContext


# === 创建 ReAct Agent ===

def create_hypothesis_agent() -> Agent:
    """创建假设验证 ReAct Agent"""
    
    agent = Agent(
        'openai:gpt-5.2',
        system_prompt='''
你是一个故障排查专家，负责验证故障假设。

工作流程:
1. Thought: 分析收集的信息，生成故障假设
2. Action: 调用验证工具获取更多信息
3. Observation: 分析验证结果
4. 循环直到确认根因或确定需要更多信息

当确认根因时，返回格式: "ROOT_CAUSE_FOUND: <根因描述>"
当需要更多信息时，返回格式: "NEED_MORE_INFO: <需要的信息类型>"
''',
        output_type=str,  # 或使用结构化输出
    )
    
    # 注册工具
    @agent.tool
    async def check_logs(
        ctx: RunContext[TroubleshootDeps],
        keywords: list[str],
        time_range: str = '1h',
    ) -> str:
        """检查 Pod 日志
        
        Args:
            keywords: 搜索关键词列表
            time_range: 时间范围 (如 '1h', '30m', '6h')
        """
        logs = await ctx.deps.log_client.query(
            ctx.deps.k8s_client.current_component,
            keywords=keywords,
            time_range=time_range,
        )
        return json.dumps(logs, indent=2)
    
    @agent.tool
    async def query_metrics(
        ctx: RunContext[TroubleshootDeps],
        metrics: list[str],
        time_range: str = '1h',
    ) -> str:
        """查询资源指标
        
        Args:
            metrics: 指标名称列表 (cpu, memory, network_io, disk_io)
            time_range: 时间范围
        """
        data = await ctx.deps.metrics_client.query_range(
            ctx.deps.k8s_client.current_component,
            metrics=metrics,
            time_range=time_range,
        )
        return json.dumps(data, indent=2)
    
    @agent.tool
    async def test_connectivity(
        ctx: RunContext[TroubleshootDeps],
        target: str,
    ) -> str:
        """测试组件连通性
        
        Args:
            target: 目标组件或地址
        """
        result = await ctx.deps.k8s_client.test_connectivity(
            ctx.deps.k8s_client.current_component,
            target=target,
        )
        return json.dumps(result, indent=2)
    
    @agent.tool
    async def describe_pod(
        ctx: RunContext[TroubleshootDeps],
        pod_name: str | None = None,
    ) -> str:
        """获取 Pod 详细信息
        
        Args:
            pod_name: Pod 名称 (可选，默认使用当前组件)
        """
        pod = pod_name or ctx.deps.k8s_client.current_component
        info = await ctx.deps.k8s_client.describe_pod(pod)
        return json.dumps(info, indent=2)
    
    @agent.tool
    async def get_events(
        ctx: RunContext[TroubleshootDeps],
        types: list[str] | None = None,
    ) -> str:
        """获取 K8s Events
        
        Args:
            types: Event 类型过滤 (Warning, Normal)
        """
        events = await ctx.deps.k8s_client.get_events(
            ctx.deps.k8s_client.current_component,
            types=types,
        )
        return json.dumps(events, indent=2)
    
    @agent.tool
    async def check_config(
        ctx: RunContext[TroubleshootDeps],
    ) -> str:
        """检查组件配置"""
        config = await ctx.deps.config_client.get_config(
            ctx.deps.k8s_client.current_component,
        )
        return json.dumps(config, indent=2)
    
    return agent
```

---

## 五、可观测性设计

### 5.1 Logfire 集成

```python
import logfire


def setup_observability():
    """设置可观测性"""
    
    logfire.configure(
        send_to_logfire='if-token-present',
    )
    
    # Instrument Pydantic AI
    logfire.instrument_pydantic_ai()
    
    # Instrument Kubernetes client
    logfire.instrument_kubernetes()
    
    # Instrument HTTP clients
    logfire.instrument_httpx()
    
    # Instrument asyncpg (如果使用 PostgreSQL)
    logfire.instrument_asyncpg()


# === 在节点中使用 ===

@dataclass
class HypothesisLoopNode(BaseNode[...]):
    async def run(self, ctx) -> ...:
        # 使用 span 追踪
        with logfire.span('hypothesis_loop', retry=ctx.state.retry_count):
            
            # Thought
            with logfire.span('generate_hypothesis'):
                hypothesis = await self._generate_hypothesis(ctx)
                logfire.info('hypothesis', description=hypothesis.description)
            
            # Action
            with logfire.span('verify_hypothesis', hypothesis_id=hypothesis.id):
                result = await self._verify_hypothesis(ctx, hypothesis)
                logfire.info('tool_calls', count=len(result['tool_results']))
            
            # Observation
            with logfire.span('analyze_result'):
                analysis = await self._analyze_verification(ctx, hypothesis, result)
                logfire.info('analysis', result=analysis.result)
```

### 5.2 关链追踪

```python
@dataclass
class TroubleshootState:
    # 添加追踪 ID
    trace_id: str = field(default_factory=lambda: str(uuid7()))
    run_id: str = field(default_factory=lambda: str(uuid7()))
    
    # 每个步骤的时间戳
    step_timestamps: dict[str, datetime] = field(default_factory=dict)


# 在每个节点记录时间戳
@dataclass  
class IntentNode(BaseNode[...]):
    async def run(self, ctx) -> ...:
        ctx.state.step_timestamps['intent_start'] = datetime.now()
        ...
        ctx.state.step_timestamps['intent_end'] = datetime.now()
```

---

## 六、断点恢复与持久化

### 6.1 状态序列化

```python
import pydantic_core


def serialize_state(state: TroubleshootState) -> str:
    """序列化状态用于持久化"""
    return pydantic_core.to_json(state).decode()


def deserialize_state(json_str: str) -> TroubleshootState:
    """反序列化状态"""
    return pydantic_core.from_json(json_str, TroubleshootState)


# === 持久化到数据库 ===

async def save_state(pool: asyncpg.Pool, state: TroubleshootState):
    """保存状态到数据库"""
    await pool.execute(
        '''
        INSERT INTO troubleshoot_sessions 
        (conversation_id, state_json, created_at, updated_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (conversation_id) DO UPDATE SET
            state_json = $2,
            updated_at = $4
        ''',
        state.conversation_id,
        serialize_state(state),
        state.created_at,
        datetime.now(),
    )


async def load_state(pool: asyncpg.Pool, conversation_id: str) -> TroubleshootState | None:
    """加载状态"""
    row = await pool.fetchrow(
        'SELECT state_json FROM troubleshoot_sessions WHERE conversation_id = $1',
        conversation_id,
    )
    if row:
        return deserialize_state(row['state_json'])
    return None
```

### 6.2 断点恢复运行

```python
async def resume_troubleshoot(
    conversation_id: str,
    deps: TroubleshootDeps,
    pool: asyncpg.Pool,
) -> FinalResult:
    """从断点恢复故障排查"""
    
    # 加载状态
    state = await load_state(pool, conversation_id)
    if not state:
        raise ValueError(f"找不到会话: {conversation_id}")
    
    # 根据 current_step 确定起始节点
    start_node_map = {
        'intent': IntentNode,
        'locate': LocateNode,
        'hypothesis': HypothesisLoopNode,
        'solution': SolutionNode,
        'validate': ValidateNode,
    }
    
    start_node_class = start_node_map.get(state.current_step, IntentNode)
    start_node = start_node_class()
    
    # 构建 Graph 并运行
    graph = build_troubleshoot_graph()
    result = await graph.run(start_node, state=state, deps=deps)
    
    # 保存最终状态
    await save_state(pool, state)
    
    return result.output
```

---

## 七、完整使用示例

```python
import asyncio
import logfire

from pydantic_ai import Agent


async def main():
    """完整故障排查示例"""
    
    # 1. 设置可观测性
    setup_observability()
    
    # 2. 初始化客户端
    k8s_client = KubernetesClient(namespace='default')
    log_client = LogClient(elasticsearch_url='http://localhost:9200')
    metrics_client = MetricsClient(prometheus_url='http://localhost:9090')
    config_client = ConfigClient()
    knowledge_base = KnowledgeBase()
    historical_cases = HistoricalCases()
    
    # 3. 创建各阶段 Agent
    intent_agent = Agent(
        'openai:gpt-5.2',
        output_type=IntentOutput,
        system_prompt='识别故障报告的意图类型...',
    )
    
    hypothesis_agent = create_hypothesis_agent()
    
    solution_agent = Agent(
        'openai:gpt-5.2',
        output_type=Solution,
        system_prompt='根据根因生成解决方案...',
    )
    
    # 4. 组装依赖
    deps = TroubleshootDeps(
        intent_agent=intent_agent,
        locate_agent=Agent('openai:gpt-5.2'),  # 用于辅助定位
        hypothesis_agent=hypothesis_agent,
        solution_agent=solution_agent,
        k8s_client=k8s_client,
        log_client=log_client,
        metrics_client=metrics_client,
        config_client=config_client,
        knowledge_base=knowledge_base,
        historical_cases=historical_cases,
    )
    
    # 5. 运行故障排查
    user_query = '''
    我的应用 nginx-ingress 突然无法访问了，返回 502 错误。
    请帮我排查一下是什么问题。
    '''
    
    result = await troubleshoot(user_query, deps)
    
    # 6. 输出报告
    print(result.to_report())
    
    # 7. 可选：持久化状态
    pool = await asyncpg.create_pool('postgresql://localhost/troubleshoot_db')
    await save_state(pool, result.state)


asyncio.run(main())
```

---

## 八、总结

### 8.1 模式选择总结

| 流程阶段 | 使用模式 | 原因 |
|---------|---------|------|
| 意图识别 | Graph Node | 固定步骤、结构化输入输出 |
| 故障定位 | Graph Node | 固定步骤、需调用工具收集信息 |
| 假设生成 → 验证 → 确认 | **ReAct 循环** | 需迭代验证、Thought-Action-Observation |
| 方案输出 | Graph Node | 固定步骤、结构化输出 |
| 验证闭环 | Human-in-the-loop | 关键决策需人工审批 |

### 8.2 设计优势

| 优势 | 说明 |
|------|------|
| **可视化流程** | Graph 可生成 Mermaid 图，便于理解排查过程 |
| **状态追踪** | 所有中间状态持久化，支持断点恢复 |
| **迭代验证** | ReAct 循环支持多轮假设验证直到收敛 |
| **可观测性** | Logfire 集成，完整追踪每个步骤 |
| **人工安全网** | 关键操作审批，防止误操作 |
| **类型安全** | 全程类型检查，防止运行时错误 |

### 8.3 适用场景

- ✅ 生产环境故障排查
- ✅ 多步骤复杂工作流
- ✅ 需要迭代验证的场景
- ✅ 需要人工决策的场景
- ✅ 需要可观测性的场景

### 8.4 与其他模式对比

| 模式 | 本场景适用性 | 原因 |
|------|------------|------|
| **纯 ReAct** | ❌ 不够 | 缺少步骤编排、状态管理 |
| **纯 Graph** | ❌ 不够 | 假设验证阶段需要迭代，不适合固定节点 |
| **Graph + ReAct** | ✅ 最佳 | Graph 提供骨架，ReAct 提供迭代验证能力 |