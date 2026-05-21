# Backend 测试脚本评估报告

> 分析日期：2026-05-21
> 分析范围：backend/ 目录下所有服务的单元测试和集成测试

---

## 一、各服务测试文件概览

| 服务 | 单元测试 | 集成测试 | conftest | 总文件数 |
|------|----------|----------|----------|----------|
| agent-service | 3 | 1 | 1 | 5 |
| api-gateway | 2 | 2 | 1 | 5 |
| case-service | 3 | 0 | 1 | 4 |
| conversation-service | 13 | 2 | 1 | 16 |
| eval-service | 1 | 1 | 1 | 3 |
| kb-service | 4 | 0 | 1 | 5 |
| scheduler-service | 2 | 2 | 1 | 5 |

---

## 二、各服务评分汇总

### 2.1 agent-service

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 | 主要问题 |
|----------|--------|--------|--------|------|----------|
| conftest.py | 2 | 4 | 3 | 9/15 | fixture 未被充分使用 |
| test_agent_port.py | 4 | 4 | 4 | 12/15 | AgentPort Protocol 未测试 |
| test_agent_router.py | **5** | 4 | **5** | 14/15 | fallback 内部细节缺失 |
| test_ai_client.py | 4 | 3 | 4 | 11/15 | **chat_completion_stream 未测试** |
| test_agent_service_api.py | 3 | 4 | 3 | 10/15 | 仅基础场景 |

**核心缺陷**：`OpenClawAssistant.chat_completion_stream()` SSE 流式调用完全未测试（覆盖率约 21%）

**改进建议**：
- 添加 `chat_completion_stream()` 测试（SSE 流式响应、端点重试、错误解析）
- 添加 `_parse_ai_error()` 状态码场景测试（401/429/500）
- 添加 AgentPort Protocol 契约验证测试

---

### 2.2 api-gateway

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 | 主要问题 |
|----------|--------|--------|--------|------|----------|
| conftest.py | **5** | **5** | **5** | 15/15 | 无问题 |
| test_gateway.py | 2 | 3 | 2 | 7/15 | 覆盖率 25%，使用 unittest 而非 pytest |
| test_terminal.py | 4 | 4 | 4 | 12/15 | SSHConnectionManager 未测试 |
| test_gateway_integration.py | 4 | **5** | 4 | 13/15 | 路由覆盖不足（3/7） |
| test_terminal_api.py | 4 | 4 | 4 | 12/15 | Task 42 功能未测试 |

**核心缺陷**：`close_case` 路由未测试（Pod 释放是核心业务逻辑）

**改进建议**：
- 添加 `close_case()` 测试（验证 Pod 释放和 Prometheus 指标）
- 统一改用 pytest 风格替代 unittest
- 添加 SSHConnectionManager 测试（连接添加/获取/移除）

---

### 2.3 case-service

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 | 主要问题 |
|----------|--------|--------|--------|------|----------|
| conftest.py | **5** | N/A | **5** | 10/15 | 基础设施 |
| test_environment_service.py | **5** | **5** | 4 | 14/15 | 优秀，字段映射测试充分 |
| test_quality_score.py | **5** | **5** | **5** | 15/15 | **优秀**，评分算法全面 |
| test_status_transitions.py | 4 | 4 | 3 | 11/15 | 状态机覆盖不足（仅 2/6 转换） |

**核心缺陷**：状态机转换测试不完整，`cancelled` 状态和非法转换未测试

**改进建议**：
- 补充完整状态机测试（所有 6 种状态转换）
- 添加非法转换测试（如 created 不能直接到 closed）
- 添加 `cancelled` 状态测试

---

### 2.4 conversation-service（测试最多）

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 |
|----------|--------|--------|--------|------|
| test_ai_client.py | 3 | 4 | 4 | 11/15 |
| test_audit_service.py | 4 | **5** | **5** | 14/15 |
| test_confirm_service.py | **5** | **5** | **5** | 15/15 |
| test_conversation_manager.py | **5** | **5** | **5** | 15/15 |
| test_diagnostic_stage_constants.py | 2 | 4 | 3 | 9/15（可合并） |
| test_diagnostic_state.py | 3 | 4 | 4 | 11/15 |
| test_evaluation_api.py | 4 | 4 | 4 | 12/15 |
| test_knowledge_retriever.py | **5** | **5** | **5** | 15/15 |
| test_prompt_audit.py | 4 | 4 | 3 | 11/15 |
| test_prompt_builder.py | 4 | 4 | **5** | 13/15 |
| test_quality_score.py | **5** | **5** | **5** | 15/15 |
| test_s0_candidate_mode.py | **5** | **5** | **5** | 15/15 |
| test_s6_resolution.py | **5** | **5** | **5** | 15/15 |
| test_conversation_integration.py | 4 | 3 | 3 | 10/15 |
| test_kb_client_contract.py | **5** | **5** | **5** | 15/15 |

**亮点**：契约测试（`test_kb_client_contract.py`）设计优秀，符合 G-2 规范

**改进建议**：
- `test_diagnostic_stage_constants.py` 可合并到其他测试文件
- `test_prompt_audit.py` fixture 层级简化，采样测试改为确定性
- 集成测试扩展 SSE 流式响应测试

---

### 2.5 eval-service

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 | 主要问题 |
|----------|--------|--------|--------|------|----------|
| test_quality_score.py | 4 | 4 | 3 | 11/15 | **QualityScoreService 未测试** |
| test_eval_service_api.py | **5** | 2 | 2 | 9/15 | **Mock 实现错误，核心逻辑未验证** |
| conftest.py | 3 | 2 | 2 | 7/15 | fixture 未被使用 |

**核心缺陷**：
1. `fetchone` mock 位置错误（应在 `execute` 返回的 Result 对象上）
2. `QualityScoreService.calculate_and_save()` 完全未测试

**改进建议**：
- **P0 修复**：修正 Mock 实现，`fetchone` 应为 `execute().return_value.fetchone`
- 添加 `QualityScoreService` 单元测试
- 添加 `QualityStatsService` 单元测试（Admin API 依赖）
- 改进 conftest.py，添加可复用的 mock fixture

---

### 2.6 kb-service

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 | 主要问题 |
|----------|--------|--------|--------|------|----------|
| test_hits.py | 4 | 4 | 4 | 12/15 | 缺少并发竞态测试 |
| test_sop_matcher.py | 4 | 4 | **5** | 13/15 | JSON 解析错误未测试 |
| test_sop_parser.py | **5** | **5** | **5** | 15/15 | **优秀** |
| test_text_splitter.py | 3 | 3 | 4 | 10/15 | chunk_overlap 未验证 |

**亮点**：`test_sop_parser.py` 是最佳测试范例，覆盖全面、断言精确

**改进建议**：
- 添加并发竞态测试（验证 `UPDATE ... RETURNING` 原子性）
- 添加 JSON 解析错误和文件读取失败场景
- 验证 chunk_overlap 实际效果

---

### 2.7 scheduler-service

| 测试文件 | 必要性 | 准确性 | 实用性 | 总评 | 主要问题 |
|----------|--------|--------|--------|------|----------|
| test_k8s_client_manifest.py | 4 | 4 | 3 | 11/15 | 仅测试 create_pod |
| test_scheduler_service.py | 4 | 3 | 3 | 10/15 | **代码重复**，reconcile 未测试 |
| test_scheduler_integration.py | 3 | 3 | 4 | 10/15 | 覆盖 3/5 端点 |
| test_scheduler_redis.py | **5** | **5** | **5** | 15/15 | **优秀**，使用 fakeredis |
| conftest.py | **5** | **5** | **5** | 15/15 | 无问题 |

**亮点**：`test_scheduler_redis.py` 使用 fakeredis 是最佳实践
**核心缺陷**：`reconcile_allocations` 方法完全未测试（服务启动核心对账）

**改进建议**：
- **P0 修复**：删除 `test_scheduler_service.py` 第 30-40 行重复代码
- 添加 `reconcile_allocations()` 测试（清理孤立分配）
- 添加 `get_available_assistants_response()` 测试（前端依赖）
- 补充 HTTP 端点测试（/assistants, /status）

---

## 三、跨服务问题汇总

### 🔴 P0 - 高优先级（必须立即修复）

| 序号 | 服务 | 问题 | 影响 |
|------|------|------|------|
| 1 | eval-service | Mock 实现错误（`fetchone` 在 session 而非 Result） | 测试实际未验证任何逻辑 |
| 2 | scheduler-service | `test_scheduler_service.py` 第 30-40 行代码重复 | 代码质量问题 |

### 🟡 P1 - 中优先级（补充核心缺失测试）

| 序号 | 服务 | 问题 | 建议 |
|------|------|------|------|
| 3 | agent-service | `chat_completion_stream()` SSE 流式调用未测试 | 添加 SSE 流式响应、端点重试、错误解析测试 |
| 4 | scheduler-service | `reconcile_allocations()` 未测试 | 添加服务启动核心对账逻辑测试 |
| 5 | api-gateway | `close_case()` 未测试 | 添加 Pod 释放和 Prometheus 指标验证 |
| 6 | eval-service | `QualityScoreService` 未测试 | 添加 calculate_and_save、update_user_rating 测试 |

### 🟢 P2 - 低优先级（扩展覆盖）

| 序号 | 服务 | 问题 |
|------|------|------|
| 7 | case-service | 状态机转换不完整（仅 2/6 转换） |
| 8 | conversation-service | fixture 层级深，可简化 |
| 9 | kb-service | 并发竞态测试缺失 |
| 10 | eval-service | conftest fixture 未被使用 |
| 11 | conversation-service | `test_diagnostic_stage_constants.py` 可合并 |

---

## 四、优秀测试范例（可作为参考）

### 4.1 kb-service/test_sop_parser.py（15/15）

**亮点**：
- 覆盖全面：所有解析路径、边界情况、warning/error
- 断言精确：验证字段值而非模糊存在性
- 结构清晰：按功能分组测试类

**可作为 SOP 解析、Markdown 解析类测试的参考模板**

### 4.2 scheduler-service/test_scheduler_redis.py（15/15）

**亮点**：
- 使用 fakeredis 替代真实 Redis，无需外部依赖
- 测试损坏 JSON 数据的自动清理
- fixture 设计合理

**可作为 Redis 依赖服务测试的参考模板**

### 4.3 conversation-service/test_kb_client_contract.py（15/15）

**亮点**：
- 消费者驱动的契约测试
- 支持模拟/集成两种模式
- 验证响应字段完整性

**符合 G-2 规范，可作为服务间契约测试的参考模板**

### 4.4 case-service/test_quality_score.py（15/15）

**亮点**：
- 纯函数测试，无外部依赖
- 边界值覆盖充分
- 权重归一化验证

**可作为评分算法、计算逻辑测试的参考模板**

---

## 五、覆盖率估算

| 服务 | 源代码行数（估算） | 已测试行数 | 覆盖率 | 评级 |
|------|--------------------|------------|--------|------|
| case-service | ~500 | ~450 | **90%** | A |
| kb-service | ~800 | ~700 | **88%** | A |
| conversation-service | ~2500 | ~2000 | **80%** | B |
| scheduler-service | ~600 | ~400 | **67%** | C |
| agent-service | ~900 | ~350 | **39%** | D |
| eval-service | ~400 | ~150 | **38%** | D |
| api-gateway | ~1100 | ~400 | **36%** | D |

---

## 六、改进计划

### Phase 1：P0 修复（已完成 ✅）

| 序号 | 任务 | 状态 | 说明 |
|------|------|------|------|
| 1 | eval-service Mock 实现修复 | ✅ 已完成 | conftest.py 添加命名空间清除逻辑 |
| 2 | scheduler-service 重复代码删除 | ✅ 已完成 | test_scheduler_service.py 删除重复命名空间处理代码 |

### Phase 2：P1 补充核心测试（已完成 ✅）

| 序号 | 任务 | 状态 | 优先级 |
|------|------|------|--------|
| 3 | agent-service: chat_completion_stream 测试 | ✅ 已完成 | 高 |
| 4 | scheduler-service: reconcile_allocations 测试 | ✅ 已完成 | 高 |
| 5 | api-gateway: close_case 测试 | ✅ 已完成 | 高 |
| 6 | eval-service: QualityScoreService 测试 | ✅ 已完成 | 高 |

### Phase 3：P2 扩展覆盖（待完成）

| 序号 | 任务 | 状态 |
|------|------|------|
| 7 | case-service: 状态机完整测试 | ⏳ 待开始 |
| 8 | conversation-service: fixture 简化 | ⏳ 待开始 |
| 9 | kb-service: 并发竞态测试 | ⏳ 待开始 |

---

## 附录：测试设计最佳实践

### A. Mock 数据库会话的正确方式

```python
# ❌ 错误：fetchone 在 session 上
mock_session.fetchone = AsyncMock(return_value=None)

# ✅ 正确：fetchone 在 Result 对象上
mock_result = AsyncMock()
mock_result.fetchone.return_value = None
mock_session.execute.return_value = mock_result
```

### B. 使用 fakeredis 替代真实 Redis

```python
import fakeredis.aioredis

@pytest.fixture
async def fake_redis():
    redis = fakeredis.aioredis.FakeRedis()
    yield redis
    await redis.close()
```

### C. 契约测试模板

```python
def test_api_response_contract():
    """验证 API 响应字段完整性"""
    response = client.get("/api/endpoint")
    assert response.status_code == 200
    data = response.json()
    
    # 验证必需字段
    required_fields = ["id", "name", "status"]
    for field in required_fields:
        assert field in data, f"缺少必需字段: {field}"
    
    # 验证字段类型
    assert isinstance(data["id"], str)
    assert isinstance(data["status"], int)
```

### D. 边界值测试模板

```python
@pytest.mark.parametrize("value,expected", [
    (0, 0),       # 最小值
    (1, 20),      # 边界值
    (5, 100),     # 正常值
    (100, 100),   # 最大值
    (-1, None),   # 非法值（异常）
])
def test_score_boundary(value, expected):
    """边界值测试"""
    if expected is None:
        with pytest.raises(ValueError):
            calculate_score(value)
    else:
        assert calculate_score(value) == expected
```