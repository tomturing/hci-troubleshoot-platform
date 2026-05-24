# CDD 效果验证方案

> 文档版本：v1.0  
> 对应模块：`backend/agent-service/app/adapters/agents/htp/case_differential.py`  
> 关联协议：`docs/agent/案例差异诊断协议.md`

---

## 一、验证目标

| 目标 | 指标 | 验收标准 |
|------|------|---------|
| 消除效率 | 步骤数 | top-K=15 候选案例 ≤ 8 步锁定到目标案例 |
| 召回率 | 正确案例是否保留 | 真实故障案例始终在 `matched_cases` 中 |
| 精确率 | 最终候选数量 | 最终候选 ≤ 30% 初始 K（≤4 个案例） |
| 健壮性 | 工具失败时不崩溃 | 连续 3 次工具失败后优雅退出，不抛异常 |
| LLM 节约 | 非流式调用次数 | 规则判断（REGEX/CONTAINS）优先，LLM 仅处理自然语言 pattern |

---

## 二、测试分层

```
tests/
├── unit/
│   ├── test_case_differential.py   ← 核心算法单元测试（含有效性验证）
│   ├── test_triage_agent.py        ← TriageAgent 意图解析测试
│   └── test_investigation_agent.py ← InvestigationAgent 路由+S4 验证
└── integration/                    ← 端到端（需要 Redis + LLM 可用，暂不自动化）
```

---

## 三、自动化单元测试

### 3.1 运行命令

```bash
cd /mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service
python -m pytest tests/unit/ -v --tb=short
```

### 3.2 关键测试用例说明

| 测试文件 | 测试类 / 方法 | 验证内容 |
|---------|-------------|---------|
| `test_case_differential.py` | `TestPickBestStep::test_picks_most_frequent_tool` | 贪心选择频率最高工具 |
| `test_case_differential.py` | `TestPickBestStep::test_excludes_already_executed_tools` | 已执行工具不重复选择 |
| `test_case_differential.py` | `TestJudgeMatchesRules::test_regex_pattern_match` | `__REGEX__` 规则判断 |
| `test_case_differential.py` | `TestJudgeMatchesRules::test_contains_pattern_match` | `__CONTAINS__` 规则判断 |
| `test_case_differential.py` | `TestDiagnoseLoop::test_eliminates_non_matching_cases` | 执行一步后过滤 c2 |
| `test_case_differential.py` | `TestDiagnoseLoop::test_definitive_match_sets_is_definitive_true` | 精确锁定设 `is_definitive=True` |
| **`test_case_differential.py`** | **`TestCDDEffectiveness::test_ten_candidates_lock_in_few_steps`** | **≤ 8 步锁定真实案例 c5（核心有效性）** |
| `test_triage_agent.py` | `TestParseIntentResult::test_confirmed_pattern` | 正则解析「已确认故障分类」 |
| `test_triage_agent.py` | `TestTriageAgentProcess::test_process_confirmed_intent_yields_stage_s1` | 确认意图后推进到 S1 |
| `test_investigation_agent.py` | `TestInvestigationAgentRouting::test_cdd_mode_yields_stage_s4` | CDD 完成后发出 S4 事件 |
| `test_agent_router.py` | `TestAgentRouterRouting::test_route_s0_to_triage_agent` | S0→TriageAgent 路由 |
| `test_agent_router.py` | `TestAgentRouterRouting::test_route_s5_to_remediation_agent` | S5→RemediationAgent 路由 |

---

## 四、有效性验证场景（TestCDDEffectiveness）

### 4.1 测试夹具设计

测试使用 10 个候选案例（5 个工具维度），其中：

- **真实故障**：案例 `c5`（Redis 服务异常导致虚拟机开机失败）  
- **干扰案例**：`c1`-`c4`、`c6`-`c10` 具有不同的工具+期望模式组合  
- **系统模拟输出**：`get_failed_tasks` 返回 `"redis service start failed"` → 快速消除无 redis 特征的案例

### 4.2 贪心消除路径（预期）

```
初始：10 候选
步骤 1: get_failed_tasks（最高频）
  → 输出含 "redis"
  → 保留 c5,c7,c9（含此工具）+ 无此步骤的案例（保守保留）
  → 消除 c2（期望 storage）、c3、c4 等
步骤 2: get_active_alerts
  → 输出不含 "network/storage/memory/cluster"
  → 消除 c1,c2,c4,c6,c10
步骤 3: acli_platform_node_list
  → 输出含 "degraded"
  → c5 匹配，c9（期望 node_count）不匹配
最终候选：c5 + 少数保守保留案例
```

### 4.3 验收标准（代码中已断言）

```python
assert len(result.steps_executed) <= 8   # 步骤数限制
assert "c5" in matched_ids               # 真实案例保留
assert len(result.matched_cases) <= 6    # 有效缩减
```

---

## 五、人工验证场景（需 LLM 环境）

> 前提：Redis 和 GLM-5 可访问，数据库中存在测试案例数据

### 场景 A：端到端 S0→S4 流程

1. 发送消息："我的虚拟机 vm-prod-01 无法开机，上午 10 点发生"
2. 期望：TriageAgent 识别为「虚拟机-003 虚拟机开机失败」或列出候选
3. 确认分类后，InvestigationAgent 执行 CDD
4. 验收：≤ 8 步内 `AgentStageUpdate(stage="S4")` 被触发，报告中包含根因

**完成标准（客观、唯一）：**
- `AgentStageUpdate.stage == "S4"` 被发出
- `AgentStageUpdate.metadata["matched_cases"]` 列表长度 ≥ 1
- 从用户输入到 S4 触发的实际工具调用次数 ≤ 8

### 场景 B：工具失败降级

1. 关闭 acli 工具连接（模拟超时）
2. 发送同上诊断请求
3. 验收：连续 3 次工具失败后 CDD 停止循环，不抛异常，输出降级报告文本

**完成标准：**
- 无 500 错误返回前端
- `CDDResult.diagnosis_report` 包含"当前信息不足以精确定位"或类似降级文本

### 场景 C：S5 修复确认流程

1. 在 S4 完成后发送"请执行修复"
2. 验收：RemediationAgent 对每个工具调用发出确认请求（`AgentInteractiveRequest`）
3. 用户确认后工具执行，拒绝后跳过

**完成标准：**
- 每次修复操作前 `AgentInteractiveRequest(kind="tool_confirm")` 被触发
- 用户确认后 `AgentStageUpdate(stage="S6")` 被发出

---

## 六、性能基准

| 指标 | 目标 | 测量方法 |
|------|------|---------|
| 首 token 延迟 | ≤ 2s | 从收到请求到首个 `AgentTextChunk` 的时间 |
| CDD 总耗时（K=15） | ≤ 30s | S1 开始到 S4 触发的时间 |
| LLM 调用次数 | ≤ 5 次 | 日志统计 `ai_client.invoke()` 调用数 |
| 内存峰值 | ≤ 100MB/session | K8s 内存监控 |

---

## 七、测试覆盖率目标

```bash
# 生成覆盖率报告
python -m pytest tests/unit/ \
  --cov=app/adapters/agents/htp \
  --cov-report=term-missing \
  --cov-fail-under=70
```

目标：`case_differential.py` 行覆盖率 ≥ 80%，`triage_agent.py` ≥ 70%。
