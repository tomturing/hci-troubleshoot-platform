# 运行时模块设计文档

> 本文档基于 `/aihci/ops-agent/ops_agent/runtime/` 目录源码分析撰写

## 1. 模块概述

运行时模块负责 SOP 知识库的加载、索引构建、检索和标记。它是 Agent 与 SOP 数据之间的桥梁。

### 1.1 模块组成

| 模块 | 功能 |
|-----|------|
| `sop_index.py` | SOP 索引管理：加载、索引构建、快速检索 |
| `sop_query.py` | SOP 查询：请求模型、会话状态、输出标准化 |
| `sop_retrieval.py` | SOP 检索：稀疏检索、候选召回 |
| `sop_markup.py` | SOP 标记：输出格式化工具 |

## 2. SOP 索引管理

### 2.1 SOPIndex 类

```python
class SOPIndex:
    """SOP 知识库索引"""
    
    def __init__(self, catalog_path: str):
        self.catalog_path = catalog_path
        self.node_map: dict[str, SOPNode] = {}      # node_path -> SOPNode
        self.branch_map: dict[str, SOPBranch] = {}  # branch_id -> SOPBranch
        self.domain_index: dict[str, list[str]] = {}  # domain -> [node_paths]
        self._build_index()
```

### 2.2 SOP 数据模型

#### SOPNode

```python
@dataclass
class SOPNode:
    node_path: str                      # 节点路径，如 "AF-VPN-SSLVPN"
    domain: str                         # 领域，如 "AF"
    title: str                          # 标题
    description: str                    # 描述
    symptoms: list[str]                 # 典型症状
    routing_signals: list[str]          # 路由信号
    alerts_or_keywords: list[str]       # 告警或关键词
    environment_scope: list[str]        # 环境范围
    branches: list[SOPBranch]           # 分支列表
```

#### SOPBranch

```python
@dataclass
class SOPBranch:
    branch_id: str                      # 分支 ID
    node_path: str                      # 所属节点路径
    title: str                          # 标题
    triggers: list[str]                 # 触发条件
    check_steps: list[SOPStep]          # 检查步骤
    solution_steps: list[SOPStep]       # 解决步骤
    risk_flags: list[str]               # 风险标记
```

#### SOPStep

```python
@dataclass
class SOPStep:
    step_id: str                        # 步骤 ID
    title: str                          # 标题
    action: str                         # 动作描述
    command_or_path: str | None         # 命令或路径
    command_example: str | None         # 命令示例
    expected_result: str                # 预期结果
    is_high_risk_operation: bool        # 是否高风险操作
    dependencies: list[str]             # 依赖步骤
```

### 2.3 索引构建

```python
def _build_index(self):
    """从 JSONL 文件构建索引"""
    with open(self.catalog_path, 'r', encoding='utf-8') as f:
        for line in f:
            record = json.loads(line.strip())
            
            # 解析节点
            node = self._parse_node(record)
            self.node_map[node.node_path] = node
            
            # 解析分支
            for branch in node.branches:
                key = f"{node.node_path}:{branch.branch_id}"
                self.branch_map[key] = branch
            
            # 构建领域索引
            domain = infer_node_domain(node.node_path)
            if domain not in self.domain_index:
                self.domain_index[domain] = []
            self.domain_index[domain].append(node.node_path)
```

### 2.4 索引查询方法

```python
def get_node(self, node_path: str) -> SOPNode:
    """获取节点"""
    if node_path not in self.node_map:
        raise KeyError(f"Node not found: {node_path}")
    return self.node_map[node_path]

def get_branch(self, node_path: str, branch_id: str) -> SOPBranch:
    """获取分支"""
    key = f"{node_path}:{branch_id}"
    if key not in self.branch_map:
        raise KeyError(f"Branch not found: {key}")
    return self.branch_map[key]

def list_domains(self) -> list[str]:
    """列出所有领域"""
    return list(self.domain_index.keys())

def list_nodes_in_domain(self, domain: str) -> list[str]:
    """列出领域内的所有节点"""
    return self.domain_index.get(domain, [])

def list_branches_in_node(self, node_path: str) -> list[SOPBranch]:
    """列出节点内的所有分支"""
    node = self.get_node(node_path)
    return node.branches
```

### 2.5 稀疏检索方法

```python
def retrieve_domains(
    self,
    problem_statement: str,
    latest_user_reply: str | None,
    confirmed_signals: list[str],
    rejected_signals: list[str],
    limit: int = 3,
) -> list[dict]:
    """检索相关领域"""
    query_text = self._build_query_text(
        problem_statement, latest_user_reply, confirmed_signals
    )
    
    scores = []
    for domain in self.list_domains():
        score = self._compute_relevance(
            query_text, domain, rejected_signals
        )
        scores.append({"domain": domain, "score": score})
    
    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores[:limit]

def retrieve_nodes(
    self,
    problem_statement: str,
    latest_user_reply: str | None,
    confirmed_signals: list[str],
    rejected_signals: list[str],
    limit: int = 4,
) -> list[dict]:
    """检索相关节点"""
    # 类似 retrieve_domains
    ...

def retrieve_routes(
    self,
    problem_statement: str,
    latest_user_reply: str | None,
    confirmed_signals: list[str],
    rejected_signals: list[str],
    limit: int = 6,
) -> list[dict]:
    """检索相关路径（route = node_path > branch_id）"""
    # 类似 retrieve_domains
    ...
```

### 2.6 相关性计算

```python
def _compute_relevance(
    self,
    query_text: str,
    target: str,
    rejected_signals: list[str],
) -> float:
    """计算查询与目标的相关性分数"""
    # 1. 关键词匹配
    query_terms = set(query_text.lower().split())
    target_terms = set(target.lower().split())
    keyword_score = len(query_terms & target_terms) / max(len(query_terms), 1)
    
    # 2. 信号匹配
    signal_score = 0.0
    # ...
    
    # 3. 负样本惩罚
    penalty = 0.0
    for rejected in rejected_signals:
        if rejected.lower() in target.lower():
            penalty += 0.2
    
    return max(0, keyword_score + signal_score - penalty)
```

## 3. SOP 查询模型

### 3.1 SOPQueryRequest

```python
@dataclass
class SOPQueryRequest:
    """SOP 检索请求"""
    query_goal: str                    # initial_route / refine_route / fallback_route / next_question
    problem_statement: str             # 问题陈述
    latest_user_reply: str | None      # 用户最近回复
    confirmed_signals: list[str]       # 已确认信号
    rejected_signals: list[str]        # 已排除信号
    excluded_routes: list[str]         # 已排除路径
    current_route: str | None          # 当前路径
    output_mode: str                   # routes_only / routes_and_questions / full_report
```

### 3.2 SOPQuerySessionState

```python
@dataclass
class SOPQuerySessionState:
    """SOP 检索会话状态"""
    request: SOPQueryRequest
    visited_ids: list[str]                      # 已访问的目标 ID
    evidence_ledger: list[dict]                 # 证据账本
    candidate_table: list[dict]                 # 候选表
    high_value_questions: list[dict]            # 高价值问题列表
    retrieval_trace: list[dict]                 # 检索轨迹
    stop_reason: str | None                     # 停止原因
    retrieval_priors: dict[str, list[dict]]     # 预取的检索先验
```

### 3.3 辅助函数

```python
def parse_scope_id(scope_id: str) -> tuple[str, str]:
    """解析范围 ID
    
    Examples:
        "root" -> ("root", "")
        "domain:AF" -> ("domain", "AF")
        "node:AF-VPN-SSLVPN" -> ("node", "AF-VPN-SSLVPN")
    """
    ...

def parse_target_id(target_id: str) -> tuple[str, str, str | None]:
    """解析目标 ID
    
    Examples:
        "node:AF-VPN-SSLVPN" -> ("node", "AF-VPN-SSLVPN", None)
        "branch:AF-VPN-SSLVPN:升级后登录失败" -> ("branch", "AF-VPN-SSLVPN", "升级后登录失败")
    """
    ...

def split_route(route: str) -> tuple[str, str]:
    """拆分路径
    
    Example:
        "AF-VPN-SSLVPN > 升级后登录失败" -> ("AF-VPN-SSLVPN", "升级后登录失败")
    """
    ...

def route_to_target_id(route: str) -> str | None:
    """路径转目标 ID
    
    Example:
        "AF-VPN-SSLVPN > 升级后登录失败" -> "branch:AF-VPN-SSLVPN:升级后登录失败"
    """
    ...
```

### 3.4 消息构建

```python
def build_query_task_message(request: SOPQueryRequest) -> str:
    """构建检索任务消息"""
    lines = [
        "## SOP Query Task",
        f"query_goal: {request.query_goal}",
        f"problem_statement: {request.problem_statement}",
        f"latest_user_reply: {request.latest_user_reply or ''}",
        f"current_route: {request.current_route or ''}",
        _section("confirmed_signals", request.confirmed_signals),
        _section("rejected_signals", request.rejected_signals),
        _section("excluded_routes", request.excluded_routes),
        f"output_mode: {request.output_mode}",
        "",
        "请开始结构化 SOP 检索。",
    ]
    return "\n".join(lines)

def build_prefetched_retrieval_hint_block(
    *,
    index: SOPIndex,
    request: SOPQueryRequest,
    priors: dict[str, list[dict]] | None,
    node_limit: int = 4,
    route_limit: int = 6,
) -> str:
    """构建预取检索提示块"""
    priors = priors or build_retrieval_priors(index=index, request=request)
    
    lines = [
        "## Prefetched Retrieval Hints",
        "这些是基于本地 sparse retrieval 自动召回的候选假设...",
    ]
    
    # 添加领域、节点、路径提示
    if priors.get("domains"):
        lines.append("top_domains:")
        for item in priors["domains"]:
            lines.append(f"- {item['domain']} | score={item['score']:.3f}")
    
    # ...
    
    return "\n".join(lines)
```

### 3.5 输出标准化

```python
def normalize_query_payload(
    payload: dict[str, Any],
    index: SOPIndex,
) -> dict[str, Any]:
    """标准化并验证子 Agent 的输出"""
    
    # 1. 验证 assessment
    assessment = payload.get("assessment", {})
    status = str(assessment.get("status", "")).strip()
    if status not in ASSESSMENT_STATUSES:
        status = "weak_match"
    
    # 2. 验证 candidates
    normalized_candidates = []
    for item in payload.get("candidates", []):
        route = str(item.get("route", "")).strip()
        node_path, branch_id = split_route(route)
        
        # 检查路径是否存在
        if node_path not in index.node_map:
            continue
        try:
            index.get_branch(node_path, branch_id)
        except KeyError:
            continue
        
        normalized_candidates.append({
            "route": f"{node_path} > {branch_id}",
            "node_path": node_path,
            "branch_id": branch_id,
            "relevance": item.get("relevance", "medium"),
            "matched_signals": _string_list(item.get("matched_signals")),
            "conflicting_signals": _string_list(item.get("conflicting_signals")),
            "missing_signals": _string_list(item.get("missing_signals")),
            "why": str(item.get("why", "")).strip(),
        })
    
    # 3. 验证 high_value_questions
    # 4. 验证 retrieval_trace
    # 5. 验证 stop_reason 和 next_actions
    
    return {
        "assessment": {"status": status, "reason": assessment.get("reason", "")},
        "candidates": normalized_candidates,
        "high_value_questions": normalized_questions,
        "retrieval_trace": normalized_trace,
        "stop_reason": stop_reason,
        "next_actions": next_actions,
    }
```

## 4. SOP 检索模块

### 4.1 检索策略

```python
def build_retrieval_priors(
    *,
    index: SOPIndex,
    request: SOPQueryRequest,
    domain_limit: int = 3,
    node_limit: int = 4,
    route_limit: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    """构建检索先验：基于 sparse retrieval 预取候选"""
    return {
        "domains": index.retrieve_domains(
            problem_statement=request.problem_statement,
            latest_user_reply=request.latest_user_reply,
            confirmed_signals=request.confirmed_signals,
            rejected_signals=request.rejected_signals,
            limit=domain_limit,
        ),
        "nodes": index.retrieve_nodes(...),
        "routes": index.retrieve_routes(...),
    }
```

### 4.2 检索流程

```
用户问题
    │
    ▼
┌─────────────────────────────────┐
│   提取查询文本                  │
│   problem_statement + signals   │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│   稀疏检索                      │
│   关键词匹配 + 信号匹配         │
└─────────────────────────────────┘
    │
    ├──────────────────────────────┐
    ▼                              ▼
┌─────────────────┐      ┌─────────────────┐
│ retrieve_domains│      │ retrieve_nodes  │
│ 领域级召回      │      │ 节点级召回      │
└─────────────────┘      └─────────────────┘
    │                              │
    └──────────────┬───────────────┘
                   ▼
         ┌─────────────────┐
         │ retrieve_routes │
         │ 路径级召回      │
         └─────────────────┘
                   │
                   ▼
         ┌─────────────────┐
         │ 排序 + 过滤     │
         │ 负样本惩罚      │
         └─────────────────┘
                   │
                   ▼
         返回候选列表
```

## 5. SOP 标记模块

### 5.1 格式化函数

```python
def section(title: str, section_id: str, content: str) -> str:
    """创建章节"""
    return f"<{section_id}>\n{title}\n{content}\n</{section_id}>"

def key_value_lines(items: list[tuple[str, Any]]) -> str:
    """创建键值对行"""
    lines = []
    for key, value in items:
        if value is not None:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)

def bullet_lines(items: list[str]) -> str:
    """创建无序列表"""
    return "\n".join(f"- {item}" for item in items)

def join_sections(*sections: str) -> str:
    """连接多个章节"""
    return "\n\n".join(s for s in sections if s.strip())
```

### 5.2 使用示例

```python
# 在工具中使用
output = join_sections(
    section(
        "Route Assessment",
        "route-assessment",
        key_value_lines([
            ("status", "need_more_evidence"),
            ("reason", "两个候选 branch 都与现象部分一致"),
        ]),
    ),
    section(
        "Candidate Route",
        "candidate-route",
        key_value_lines([
            ("rank", 1),
            ("route", "AF-VPN-SSLVPN > 升级后登录失败"),
            ("relevance", "high"),
            ("matched-signals", "升级后出现 / Web 门户登录失败"),
            ("why", "升级时间窗口与该 branch 的典型触发条件吻合"),
        ]),
    ),
    section(
        "Next Actions",
        "next-actions",
        bullet_lines([
            "向用户追问影响范围",
            "若用户回答全量受影响，则直接进入 branch-A 的 validation",
        ]),
    ),
)
```

## 6. 设计亮点

### 6.1 三级索引结构

```
Domain (领域)
    └── Node (节点)
            └── Branch (分支)
                    └── Step (步骤)
```

这种结构支持：
- 粗粒度检索：快速定位领域
- 细粒度展开：逐步深入到具体步骤

### 6.2 预取加速

- 在检索开始前，基于 sparse retrieval 预取候选
- Agent 优先验证高相关性候选
- 避免盲目广度搜索

### 6.3 会话状态管理

- `SOPQuerySessionState` 维护完整的检索状态
- 支持多轮检索的上下文传递
- 证据账本记录所有匹配/冲突/缺失信号

### 6.4 输出标准化

- `normalize_query_payload()` 确保输出格式一致
- 路径验证确保候选真实存在
- 缺失字段自动补默认值

## 7. 与 Agent 的协作

```
OpsAgent
    │
    │ query_sop_candidates 工具
    │
    ▼
SOPQuerySubAgent
    │
    │ new_task() 初始化
    │
    ├─ load_sop_index() → SOPIndex
    │
    ├─ build_retrieval_priors() → 预取候选
    │
    └─ 构建初始消息
            │
            │ 检索过程中
            │
            ├─ sop_query_open_index → SOPIndex.list_domains/nodes
            │
            ├─ sop_query_read_target → SOPIndex.get_node/get_branch
            │
            ├─ sop_query_compare_candidates → SOPIndex + 证据账本
            │
            └─ sub_agent_task_done
                    │
                    ▼
            normalize_query_payload()
                    │
                    ▼
            返回给 OpsAgent
```
