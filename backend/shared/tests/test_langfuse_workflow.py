"""共享 Langfuse 业务观测封装测试。"""

from unittest.mock import MagicMock, patch

from shared.observability.langfuse import (
    _capture_content_for_operation,
    _content_summary,
    _current_workflow_observation,
    observe_llm_generation,
    observe_workflow,
)


def test_content_can_be_reduced_to_hash_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "false")

    summary = _content_summary({"prompt": "客户现场日志", "api_key": "secret"})

    assert summary["content_redacted"] is True
    assert summary["content_chars"] > 0
    assert len(summary["content_sha256"]) == 64


def test_extract_signals_content_can_be_enabled_independently(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("LANGFUSE_CAPTURE_EXTRACT_SIGNALS_CONTENT", "true")

    assert _capture_content_for_operation("extract_signals") is True
    assert _capture_content_for_operation("vision") is False
    assert _content_summary({"prompt": "故障正文"}, capture_content=True) == {"prompt": "故障正文"}


def test_extract_signals_content_defaults_to_visible_when_dedicated_setting_is_absent(monkeypatch):
    monkeypatch.delenv("LANGFUSE_CAPTURE_CONTENT", raising=False)
    monkeypatch.delenv("LANGFUSE_CAPTURE_EXTRACT_SIGNALS_CONTENT", raising=False)

    assert _capture_content_for_operation("extract_signals") is True


def test_all_business_operations_default_to_visible(monkeypatch):
    monkeypatch.delenv("LANGFUSE_CAPTURE_CONTENT", raising=False)
    monkeypatch.delenv("LANGFUSE_CAPTURE_VISION_CONTENT", raising=False)

    assert _capture_content_for_operation("vision") is True
    assert _capture_content_for_operation("classify") is True
    assert _content_summary({"result": "实际结果"}) == {"result": "实际结果"}


def test_extract_signals_content_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_EXTRACT_SIGNALS_CONTENT", "false")

    assert _capture_content_for_operation("extract_signals") is False


def test_generation_is_nested_under_workflow():
    root = MagicMock()
    generation = MagicMock()
    root.start_observation.return_value = generation

    def start_explicit(**_kwargs):
        return root.start_observation() if _current_workflow_observation.get() is root else root

    with patch("shared.observability.langfuse._start_explicit_observation", side_effect=start_explicit):
        with observe_workflow(name="kbd.pipeline", session_id="run-1"):
            with observe_llm_generation(operation="classify", model="model-a", input={"prompt": "x"}):
                pass

    root.start_observation.assert_called_once()
    generation.end.assert_called_once()
    root.end.assert_called_once()


def test_workflow_forwards_explicit_trace_id():
    root = MagicMock()

    with patch(
        "shared.observability.langfuse._start_explicit_observation",
        return_value=root,
    ) as start_explicit:
        with observe_workflow(name="kbd.batch", trace_id="a" * 32):
            pass

    assert start_explicit.call_args.kwargs["trace_id"] == "a" * 32
