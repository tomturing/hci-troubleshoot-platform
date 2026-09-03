"""
backend/kb-service/tests/test_signal_extraction_orchestrator.py
测试多 Agent 分层协同调度器各阶段行为、状态机流转与自愈闭环。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.signal_orchestrator import SignalExtractionOrchestrator


@pytest.mark.asyncio
async def test_signal_orchestrator_count_agent_success():
    """测试计数 Agent 正常提取意图"""
    mock_db = MagicMock()
    mock_session = AsyncMock()

    async def mock_llm(prompt: str, stage: str):
        assert stage == "count"
        return {
            "signal_count": 2,
            "intents": [
                {"intent_id": "intent_001", "role_type": "producer", "core_entity": "创建虚拟机失败", "evidence_raw": "任务失败"},
                {"intent_id": "intent_002", "role_type": "consumer", "core_entity": "查日志", "evidence_raw": "查看 task.log"}
            ]
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Prompt: {composite_text} {steps_text}"
        count, intents = await orchestrator.run_count_agent(mock_session, 1001, "标题内容", "步骤内容")

    assert count == 2
    assert len(intents) == 2
    assert intents[0]["role_type"] == "producer"


@pytest.mark.asyncio
async def test_signal_orchestrator_count_agent_uncountable_failure():
    """测试计数 Agent 异常时落库 failure_log"""
    mock_db = MagicMock()
    mock_session = AsyncMock()

    async def mock_llm(prompt: str, stage: str):
        return {"signal_count": 0, "intents": [], "uncountable_reason": "文本无结构"}

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Prompt: {composite_text} {steps_text}"
        with patch("app.services.signal_asset_service.SignalAssetService.record_failure", new_callable=AsyncMock) as mock_fail:
            count, intents = await orchestrator.run_count_agent(mock_session, 1002, "空标题", "空步骤")

    assert count == 0
    assert len(intents) == 0
    mock_fail.assert_called_once()
    assert mock_fail.call_args[1]["reason"] == "UNCOUNTABLE"


@pytest.mark.asyncio
async def test_signal_orchestrator_classify_agent_adversarial():
    """测试分类 Agent 对抗审查映射到受限 Catalog"""
    mock_db = MagicMock()
    mock_session = AsyncMock()

    async def mock_llm(prompt: str, stage: str):
        assert stage == "classify"
        return {
            "tool_name": "qkv_task",
            "category": "frontend",
            "rationale": "命中任务操作失败特征",
            "confidence": 0.98
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    intent = {"core_entity": "创建虚拟机失败", "evidence_raw": "任务失败"}
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Classify: {core_entity} {evidence_raw} {composite_text} {steps_text} {acquirer_catalog} {category_baseline}"
        res = await orchestrator.run_classify_agent(
            mock_session, 1003, intent, "复合上下文", "步骤上下文", "目录", "基线"
        )

    assert res["valid"] is True
    assert res["tool_name"] == "qkv_task"
    assert res["category"] == "frontend"


@pytest.mark.asyncio
async def test_signal_orchestrator_classify_agent_unclassified():
    """测试分类为非法工具时标记 unclassified 并落库"""
    mock_db = MagicMock()
    mock_session = AsyncMock()

    async def mock_llm(prompt: str, stage: str):
        return {"tool_name": "invalid_tool_unknown"}

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    intent = {"core_entity": "未知设备操作", "evidence_raw": "无法理解的内容"}
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Classify: {core_entity} {evidence_raw} {composite_text} {steps_text} {acquirer_catalog} {category_baseline}"
        with patch("app.services.signal_asset_service.SignalAssetService.record_failure", new_callable=AsyncMock) as mock_fail:
            res = await orchestrator.run_classify_agent(
                mock_session, 1004, intent, "复合", "步骤", "目录", "基线"
            )

    assert res["valid"] is False
    assert res["tool_name"] == "unclassified"
    mock_fail.assert_called_once()
    assert mock_fail.call_args[1]["reason"] == "UNCLASSIFIED"


@pytest.mark.asyncio
async def test_signal_orchestrator_model_agent_with_best_practice():
    """测试建模 Agent 注入最佳实践黄金案例"""
    mock_db = MagicMock()
    mock_session = AsyncMock()

    async def mock_llm(prompt: str, stage: str):
        assert stage == "model"
        return {
            "id": "sig_001",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "新建虚拟机", "is_failed": True}},
            "orchestrate": {"phase": "diagnostic", "produces": [{"name": "VM", "path": "vm"}], "requires": []}
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    classified = {
        "tool_name": "qkv_task",
        "intent": {"core_entity": "新建虚拟机失败", "evidence_raw": "任务报错"}
    }
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Model: {tool_name} {core_entity} {evidence_raw} {shared_variables} {best_practices} {acli_catalog}"
        with patch("app.services.signal_asset_service.SignalAssetService.get_best_practices_by_tool", new_callable=AsyncMock) as mock_bp:
            mock_bp.return_value = [
                {"pattern_category": "任务失败", "signal_json": {"id": "ref_1"}, "design_notes": "参考notes"}
            ]
            sig = await orchestrator.run_model_agent(mock_session, 1005, classified, "Catalog")

    assert sig is not None
    assert sig["acquire"]["tool"] == "qkv_task"
    assert sig["orchestrate"]["produces"][0]["name"] == "VM"


@pytest.mark.asyncio
async def test_signal_orchestrator_verify_and_self_healing():
    """测试验证 Agent 全局对账与门禁自愈闭环"""
    mock_db = MagicMock()
    mock_session = AsyncMock()

    # 模拟第一轮门禁拦截报错，第二轮自愈后通过
    attempts = 0

    def mock_gate_checker(signals):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # 第一轮：有 issue
            return (
                [],
                [{"signal": signals[0], "reason": "日志路径包含日期占位符"}],
                ["日志路径不可解析: 包含 <日期>"]
            )
        else:
            # 第二轮（自愈后）：通过
            return (signals, [], [])

    async def mock_llm(prompt: str, stage: str):
        assert stage == "verify"
        return {
            "verification_status": "passed",
            "signals": [
                {
                    "id": "sig_001",
                    "acquire": {"tool": "qfk_log", "args": {"file": "vtp.log", "path": "/sf/log/today"}},
                    "match": {"type": "keyword", "pattern": ["ERR"]}
                }
            ]
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    bad_signal = {
        "id": "sig_001",
        "acquire": {"tool": "qfk_log", "args": {"file": "vtp.log", "path": "/sf/log/<日期>"}}
    }
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Verify: {signals_json} {rejected_candidates} {raw_count} {kbd_context} {gate_issues}"
        validated, rejected = await orchestrator.run_verify_and_self_heal(
            mock_session, 1006, 1, [bad_signal], [], "KBD全文", mock_gate_checker
        )

    assert attempts == 2  # 触发了一轮自愈
    assert len(validated) == 1
    assert validated[0]["acquire"]["args"]["path"] == "/sf/log/today"
