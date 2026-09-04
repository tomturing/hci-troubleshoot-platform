"""
backend/kb-service/tests/test_signal_extraction_orchestrator.py
测试多 Agent 分层协同调度器各阶段行为、状态机流转与自愈闭环。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.signal_orchestrator import SignalExtractionOrchestrator, discover_signal_candidates

from scripts.evaluate_multi_agent_extraction import (
    DEFAULT_PROMPT_MIGRATION,
    PROMPT_NAMES,
    _agreement_status,
    _build_summary,
    _diff_signals,
    _evaluation_status,
    _load_candidate_prompts,
)


def test_evaluator_separates_pipeline_completion_from_expert_agreement():
    """管线完整通过不等于与专家一致，两个指标必须独立。"""
    diff = {
        "expert_count": 3,
        "predicted_count": 3,
        "exact_key_matches": 1,
        "count_matches": True,
        "tool_matches": 2,
        "missing_from_prediction": [["qfk_vm"]],
        "extra_in_prediction": [["qkv_task"]],
    }

    assert _evaluation_status(3, 3, 0) == "ok"
    assert _agreement_status(diff) == "mismatch"


def test_evaluator_summary_uses_only_completed_gold_comparisons():
    """错误样本不应被当成零信号拉低 Gold Label 分母。"""
    records = [
        {
            "status": "failed",
            "agreement_status": "mismatch",
            "rejected_count": 1,
            "rejected": [{"reason": "结构错误"}],
            "diff": {
                "expert_count": 2,
                "predicted_count": 1,
                "exact_key_matches": 1,
                "count_matches": False,
                "tool_matches": 1,
                "missing_from_prediction": [["qfk_log"]],
                "extra_in_prediction": [],
            },
        },
        {"status": "error", "error": "TimeoutError"},
    ]

    summary = _build_summary(records)

    assert summary["pipeline_status_counts"] == {"ok": 0, "failed": 1, "error": 1}
    assert summary["signal_totals"]["expert"] == 2
    assert summary["micro_precision"] == 1.0
    assert summary["micro_recall"] == 0.5
    assert summary["failure_reasons"] == {"结构错误": 1}


def test_evaluator_signal_diff_preserves_duplicate_gold_labels():
    """相同 key 的多条专家信号必须按出现次数对账，不能被 set 吞掉。"""
    signal = {"acquire": {"tool": "qfk_system", "args": {"command": "ls"}}, "match": {"type": "exists"}}
    diff = _diff_signals([signal, signal], [signal])

    assert diff["expert_count"] == 2
    assert diff["exact_key_matches"] == 1
    assert len(diff["missing_from_prediction"]) == 1


def test_evaluator_loads_candidate_prompts_from_current_pr_migration():
    """PR 效果验证必须使用候选 Prompt，而不是误用 staging 旧版本。"""
    prompts = _load_candidate_prompts(DEFAULT_PROMPT_MIGRATION)

    assert set(prompts) == set(PROMPT_NAMES)
    assert "禁止使用 TODAY/YMD 等未注册别名" in prompts["kbd_signal_model_v1"]


def test_rule_candidate_discovery_covers_failure_command_and_log_evidence():
    """候选发现必须覆盖故障短语、命令和日志证据，且保留原文。"""
    candidates = discover_signal_candidates(
        "创建虚拟机失败；网络连接超时",
        "1. 执行 acli vm get vm-1\n2. 查看 /sf/log/vtp.log 并 grep ERROR",
    )

    assert len(candidates) == 4
    assert {item["role_type"] for item in candidates} == {"producer", "consumer"}
    assert all(item["evidence_raw"] for item in candidates)
    assert all(item["discovery_method"] == "rule" for item in candidates)


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
                {
                    "intent_id": "intent_001",
                    "role_type": "producer",
                    "core_entity": "创建虚拟机失败",
                    "evidence_raw": "任务失败",
                    "source_kind": "composite",
                },
                {
                    "intent_id": "intent_002",
                    "role_type": "consumer",
                    "core_entity": "查日志",
                    "evidence_raw": "查看 task.log",
                    "source_kind": "steps",
                },
            ],
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Prompt: {composite_text} {steps_text}"
        count, intents = await orchestrator.run_count_agent(
            mock_session, 1001, "标题内容 任务失败", "步骤内容 查看 task.log"
        )

    assert count == 2
    assert len(intents) == 2
    assert intents[0]["role_type"] == "producer"


@pytest.mark.asyncio
async def test_pipeline_passes_diagnostic_image_evidence_to_count_agent():
    """多 Agent 路径必须接收诊断图片筛选后的 OCR 证据。"""
    seen: dict[str, str] = {}

    async def mock_llm(prompt: str, stage: str):
        if stage == "count":
            seen["prompt"] = prompt
            return {
                "signal_count": 1,
                "intents": [{"role_type": "producer", "core_entity": "任务失败", "evidence_raw": "任务失败"}],
            }
        if stage == "classify":
            return {"tool_name": "qkv_task", "confidence": 0.9}
        return {
            "id": "model-id",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "任务失败"}},
            "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
            "provenance": {"evidence": "任务失败"},
        }

    def gate_checker(signals):
        return signals, [], []

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    templates = {
        "kbd_signal_count_v1": "{composite_text} {steps_text}",
        "kbd_signal_classify_v1": "{core_entity} {evidence_raw} {composite_text} {steps_text} {acquirer_catalog} {category_baseline}",
        "kbd_signal_model_v1": "{tool_name} {core_entity} {evidence_raw} {shared_variables} {best_practices} {acli_catalog}",
        "kbd_signal_verify_v1": "{signals_json} {rejected_candidates} {raw_count} {kbd_context} {gate_issues}",
    }
    orchestrator.prompt_templates = templates
    with patch.object(orchestrator, "_get_best_practices", new_callable=AsyncMock, return_value=[]):
        validated, rejected, _ = await orchestrator.extract_kbd_signals_pipeline(
            AsyncMock(),
            1013,
            {"title": "", "problem_description": "", "alert_info": "任务失败", "steps_text": "", "category_id": ""},
            "目录",
            "acli",
            gate_checker,
            "截图 OCR：任务失败",
        )

    assert "截图 OCR：任务失败" in seen["prompt"]
    assert len(validated) == 1
    assert rejected == []


@pytest.mark.asyncio
async def test_count_agent_deduplicates_and_prefers_steps_evidence():
    """复合字段与步骤重复时只保留步骤信号，并以确定性数量为准。"""

    async def mock_llm(prompt: str, stage: str):
        return {
            "signal_count": 2,
            "intents": [
                {
                    "role_type": "producer",
                    "source_kind": "composite",
                    "core_entity": "查看任务失败",
                    "evidence_raw": "查看任务失败",
                },
                {
                    "role_type": "consumer",
                    "source_kind": "steps",
                    "core_entity": "查看任务失败",
                    "evidence_raw": "步骤中查看任务失败",
                },
            ],
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Prompt: {composite_text} {steps_text}"
        with patch.object(orchestrator, "_record_failure", new_callable=AsyncMock) as mock_failure:
            count, intents = await orchestrator.run_count_agent(AsyncMock(), 1007, "查看任务失败", "步骤中查看任务失败")

    assert count == 1
    assert intents[0]["source_kind"] == "steps"
    assert mock_failure.call_args.kwargs["reason"] == "COUNT_MISMATCH"


@pytest.mark.asyncio
async def test_count_agent_rejects_ungrounded_evidence():
    """计数 Agent 不能凭空生成原文不存在的证据。"""

    async def mock_llm(prompt: str, stage: str):
        return {
            "signal_count": 1,
            "intents": [
                {
                    "role_type": "producer",
                    "source_kind": "composite",
                    "core_entity": "虚拟机失败",
                    "evidence_raw": "原文不存在的失败证据",
                }
            ],
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Prompt: {composite_text} {steps_text}"
        with patch.object(orchestrator, "_record_failure", new_callable=AsyncMock) as mock_failure:
            count, intents = await orchestrator.run_count_agent(AsyncMock(), 1008, "虚拟机异常", "查看日志")

    assert (count, intents) == (0, [])
    assert mock_failure.call_args.kwargs["reason"] == "COUNT_EVIDENCE_UNGROUNDED"


@pytest.mark.asyncio
async def test_count_agent_keeps_valid_intents_when_one_candidate_is_malformed():
    """单个坏候选只被拒绝，其他可追溯候选仍继续下游。"""

    async def mock_llm(prompt: str, stage: str):
        return {
            "signal_count": 2,
            "intents": [
                {
                    "role_type": "producer",
                    "core_entity": "任务失败",
                    "evidence_raw": "任务失败",
                    "source_kind": "composite",
                },
                {"role_type": "producer", "core_entity": "", "evidence_raw": "凭空内容", "source_kind": "composite"},
            ],
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "{composite_text} {steps_text}"
        with patch.object(orchestrator, "_record_failure", new_callable=AsyncMock):
            count, intents = await orchestrator.run_count_agent(AsyncMock(), 1011, "任务失败", "")

    assert count == 1
    assert intents[0]["core_entity"] == "任务失败"


@pytest.mark.asyncio
async def test_pipeline_passes_content_without_field_labels():
    """字段名不能泄漏给 Agent，避免“告警信息”被当成 qkv_alert 证据。"""
    seen = {}

    async def mock_llm(prompt: str, stage: str):
        if stage == "count":
            seen["prompt"] = prompt
            return {
                "signal_count": 1,
                "intents": [
                    {
                        "role_type": "producer",
                        "core_entity": "任务失败",
                        "evidence_raw": "任务失败",
                        "source_kind": "composite",
                    }
                ],
            }
        if stage == "classify":
            return {"tool_name": "qkv_task", "confidence": 0.9}
        return {"acquire": {"tool": "qkv_task", "args": {"keyword": "任务失败"}}, "orchestrate": {}}

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:

        async def load_prompt(*args, **kwargs):
            return "{composite_text} {steps_text}"

        mock_load.side_effect = load_prompt
        # 只验证计数阶段输入构造；后续门禁依赖完整真实信号契约。
        await orchestrator.run_count_agent(AsyncMock(), 1010, "标题 任务失败", "步骤")

    assert "【标题】" not in seen["prompt"]
    assert "【告警信息】" not in seen["prompt"]


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
        with patch(
            "app.services.signal_asset_service.SignalAssetService.record_failure", new_callable=AsyncMock
        ) as mock_fail:
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
            "confidence": 0.98,
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
        with patch(
            "app.services.signal_asset_service.SignalAssetService.record_failure", new_callable=AsyncMock
        ) as mock_fail:
            res = await orchestrator.run_classify_agent(mock_session, 1004, intent, "复合", "步骤", "目录", "基线")

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
            "orchestrate": {"phase": "diagnostic", "produces": [{"name": "VM", "path": "vm"}], "requires": []},
            "provenance": {"evidence": "任务报错"},
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    classified = {
        "tool_name": "qkv_task",
        "intent": {
            "candidate_id": "kbd_1005_candidate_001",
            "core_entity": "新建虚拟机失败",
            "evidence_raw": "任务报错",
        },
    }
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = (
            "Model: {tool_name} {core_entity} {evidence_raw} {shared_variables} {best_practices} {acli_catalog}"
        )
        with patch(
            "app.services.signal_asset_service.SignalAssetService.get_best_practices_by_tool", new_callable=AsyncMock
        ) as mock_bp:
            mock_bp.return_value = [
                {"pattern_category": "任务失败", "signal_json": {"id": "ref_1"}, "design_notes": "参考notes"}
            ]
            sig = await orchestrator.run_model_agent(mock_session, 1005, classified, "Catalog")

    assert sig is not None
    assert sig["id"] == "kbd_1005_candidate_001"
    assert sig["acquire"]["tool"] == "qkv_task"
    assert sig["orchestrate"]["produces"][0]["name"] == "VM"


@pytest.mark.asyncio
async def test_model_agent_rejects_few_shot_evidence_not_bound_to_candidate():
    """最佳实践中的外部证据不能替代当前候选原文。"""

    async def mock_llm(prompt: str, stage: str):
        return {
            "id": "sig_foreign",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "外部案例关键词"}},
            "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
            "provenance": {"evidence": "外部案例关键词"},
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    classified = {
        "tool_name": "qkv_task",
        "intent": {"candidate_id": "kbd_1_candidate_001", "core_entity": "任务失败", "evidence_raw": "任务失败"},
    }
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = (
            "{tool_name} {core_entity} {evidence_raw} {shared_variables} {best_practices} {acli_catalog}"
        )
        with patch.object(orchestrator, "_get_best_practices", new_callable=AsyncMock, return_value=[]):
            with patch.object(orchestrator, "_record_failure", new_callable=AsyncMock) as mock_failure:
                result = await orchestrator.run_model_agent(AsyncMock(), 1012, classified, "Catalog")

    assert result is None
    assert mock_failure.call_args.kwargs["reason"] == "MODEL_AGENT_EXCEPTION"


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
            return ([], [{"signal": signals[0], "reason": "日志路径包含日期占位符"}], ["日志路径不可解析: 包含 <日期>"])
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
                    "match": {"type": "keyword", "pattern": ["ERR"]},
                }
            ],
        }

    orchestrator = SignalExtractionOrchestrator(mock_db, llm_caller=mock_llm)
    bad_signal = {
        "id": "sig_001",
        "acquire": {"tool": "qfk_log", "args": {"file": "vtp.log", "path": "/sf/log/<日期>"}},
    }
    with patch("shared.utils.prompt_loader.StrictPromptLoader.load_and_validate", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "Verify: {signals_json} {rejected_candidates} {raw_count} {kbd_context} {gate_issues}"
        validated, rejected = await orchestrator.run_verify_and_self_heal(
            mock_session, 1006, 1, [bad_signal], [], "KBD全文", mock_gate_checker
        )

    assert attempts == 2  # 触发了一轮自愈
    assert len(validated) == 1
    assert validated[0]["acquire"]["args"]["path"] == "/sf/log/today"


@pytest.mark.asyncio
async def test_verify_rejects_partial_self_heal_even_if_it_improves_count():
    """自愈只改善部分候选但仍未对账时不得放行。"""
    attempts = 0

    def mock_gate_checker(signals):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return [], [{"signal": signals[0], "reason": "invalid"}], ["invalid"]
        return signals, [], []

    async def mock_llm(prompt: str, stage: str):
        return {
            "verification_status": "passed",
            "signals": [{"id": "sig_001", "acquire": {"tool": "qkv_task", "args": {"keyword": "失败"}}}],
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = "{signals_json} {rejected_candidates} {raw_count} {kbd_context} {gate_issues}"
        with patch.object(orchestrator, "_record_failure", new_callable=AsyncMock):
            validated, rejected = await orchestrator.run_verify_and_self_heal(
                AsyncMock(), 1009, 2, [{"id": "bad"}], [], "KBD", mock_gate_checker
            )

    assert validated == []
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_model_agent_dag_dynamic_variable_closure():
    """验证 Producer 产出变量能动态注入到 Consumer 建模上下文，实现闭包。"""
    prompts_seen = {}

    async def mock_llm(prompt: str, stage: str):
        if "qkv_task" in prompt:
            return {
                "id": "sig_001",
                "acquire": {"tool": "qkv_task", "args": {"keyword": "磁盘坏道"}},
                "orchestrate": {"phase": "diagnostic", "produces": [{"name": "DISK_ID", "alias": "DISK_ID"}]},
                "provenance": {"evidence": "磁盘坏道报错"},
            }
        # 记录消费者建模收到的 prompt
        prompts_seen["consumer_prompt"] = prompt
        return {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "smartctl -a /dev/{{DISK_ID}}"}},
            "orchestrate": {"phase": "diagnostic", "requires": ["DISK_ID"]},
            "provenance": {"evidence": "执行 smartctl 检查磁盘"},
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    entry_data = {
        "title": "磁盘坏道故障",
        "problem_description": "磁盘坏道报错",
        "alert_info": "",
        "steps_text": "执行 smartctl 检查磁盘",
    }

    # 模拟分类结果包含 1 个 Producer 和 1 个 Consumer
    with patch.object(orchestrator, "run_count_agent", new_callable=AsyncMock) as mock_count:
        mock_count.return_value = (
            2,
            [
                {"core_entity": "磁盘坏道", "evidence_raw": "磁盘坏道报错", "role_type": "producer"},
                {"core_entity": "检查磁盘", "evidence_raw": "执行 smartctl 检查磁盘", "role_type": "consumer"},
            ],
        )
        with patch.object(orchestrator, "run_classify_agent", new_callable=AsyncMock) as mock_classify:
            mock_classify.side_effect = [
                {
                    "valid": True,
                    "tool_name": "qkv_task",
                    "intent": {"candidate_id": "c1", "evidence_raw": "磁盘坏道报错"},
                },
                {
                    "valid": True,
                    "tool_name": "qfk_system",
                    "intent": {"candidate_id": "c2", "evidence_raw": "执行 smartctl 检查磁盘"},
                },
            ]
            with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
                mock_load.return_value = (
                    "{tool_name} {core_entity} {evidence_raw} {shared_variables} {best_practices} {acli_catalog}"
                )
                with patch.object(orchestrator, "_get_best_practices", new_callable=AsyncMock, return_value=[]):

                    def dummy_gate_checker(sigs):
                        return sigs, [], []

                    validated, rejected, raw_count = await orchestrator.extract_kbd_signals_pipeline(
                        None, 2001, entry_data, "catalog", "acli", dummy_gate_checker
                    )

    assert len(validated) == 2
    # 验证 Consumer 接收到了 Producer 动态产出的 DISK_ID 变量
    assert "DISK_ID" in prompts_seen.get("consumer_prompt", "")
    assert validated[1]["acquire"]["args"]["command"] == "smartctl -a /dev/{{DISK_ID}}"


@pytest.mark.asyncio
async def test_model_agent_evidence_grounding_auto_repair_and_requires_cleaning():
    """验证证据轻微改写时自动纠偏回填候选原文，且清洗悬空变量。"""

    async def mock_llm(prompt: str, stage: str):
        return {
            "id": "sig_001",
            "acquire": {"tool": "qfk_storage", "args": {"command": "acli storage pool info"}},
            # 模型自行臆造了悬空变量 VM_DISK_PATH
            "orchestrate": {"phase": "diagnostic", "requires": ["HOST", "VM_DISK_PATH"]},
            # 模型轻微改写了证据（少了空格，但核心字符匹配）
            "provenance": {"evidence": "存储池异常"},
        }

    orchestrator = SignalExtractionOrchestrator(MagicMock(), llm_caller=mock_llm)
    classified = {
        "tool_name": "qfk_storage",
        "intent": {
            "candidate_id": "kbd_3001_candidate_001",
            "core_entity": "存储池状态异常",
            "evidence_raw": "存储池 状态 异常",
        },
    }
    with patch.object(orchestrator, "_load_prompt", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = (
            "{tool_name} {core_entity} {evidence_raw} {shared_variables} {best_practices} {acli_catalog}"
        )
        with patch.object(orchestrator, "_get_best_practices", new_callable=AsyncMock, return_value=[]):
            sig = await orchestrator.run_model_agent(AsyncMock(), 3001, classified, "Catalog")

    assert sig is not None
    # 验证证据被自动安全纠偏回填为候选原文
    assert sig["provenance"]["evidence"] == "存储池 状态 异常"
    # 验证未闭合且未被引用的悬空变量 VM_DISK_PATH 被清洗剔除，保留合法的 HOST
    assert sig["orchestrate"]["requires"] == ["HOST"]


@pytest.mark.asyncio
async def test_producer_end_variable_auto_derives_date_variable():
    """验证生产者信号（qkv_alert/qkv_task）产出 END 时，自动派生 DATE 变量并闭环注入下游。"""
    from app.routes.extract_signals import _normalize_derived_date_variables, _validate_and_collect_signals

    # 1. 单测 _normalize_derived_date_variables
    signals = [
        {
            "id": "sig_task",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "delete"}},
            "orchestrate": {"produces": [{"name": "END", "path": "end"}]},
        },
        {
            "id": "sig_log",
            "acquire": {"tool": "qfk_log", "args": {"file": "vtp.log", "time_window": "{{DATE}}"}},
            "orchestrate": {"requires": ["HOST", "DATE"]},
            "match": {"type": "keyword", "pattern": "error"},
            "provenance": {"category": "backend", "evidence": "日志报错"},
        },
    ]

    count = _normalize_derived_date_variables(signals)
    assert count == 1
    task_produces = signals[0]["orchestrate"]["produces"]
    names = [p["name"] for p in task_produces]
    assert "END" in names
    assert "DATE" in names

    # 2. 端到端 DAG 闭包验证
    full_signals = [
        {
            "id": "sig_001",
            "role": "must",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "delete"}},
            "orchestrate": {
                "phase": "diagnostic",
                "produces": [{"name": "HOST", "path": "host"}, {"name": "END", "path": "end"}],
            },
            "provenance": {"category": "frontend", "evidence": "删除失败"},
        },
        {
            "id": "sig_002",
            "role": "must",
            "acquire": {"tool": "qfk_log", "args": {"file": "vtp.log", "time_window": "{{DATE}}"}},
            "match": {"type": "keyword", "pattern": "error", "expected": True},
            "orchestrate": {"phase": "diagnostic", "requires": ["HOST", "DATE"]},
            "provenance": {"category": "backend", "evidence": "vtpdaemon error 发生故障"},
        },
    ]
    validated, rejected = _validate_and_collect_signals(full_signals, source_id="kbd_test")
    assert len(rejected) == 0
    assert len(validated) == 2
    # 验证 sig_001 获得了 DATE 产出，sig_002 顺利消费了 DATE
    sig1_produces = validated[0]["orchestrate"]["produces"]
    assert any(p.get("name") == "DATE" for p in sig1_produces)
