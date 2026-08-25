"""qkv_vm_console 共享契约回归：VmConsoleResolver、近黑检测与视觉观察 Schema。

对齐设计文档 §11.4：Schema/适配器只产生固定两种 operation；任意参数不能改变
Monitor 指令或临时路径；词表外值降级 unknown；低置信度不输出根因。
"""

import pytest
from pydantic import ValidationError
from shared.resolution.models import ResolutionStatus, SignalIntent
from shared.resolution.vm_console import (
    VM_CONSOLE_OPERATIONS,
    VmConsoleCaptureIntent,
    VmConsoleResolver,
    build_wake_intent,
    capture_intent_from,
)
from shared.vision.near_black import (
    NEAR_BLACK_ALGORITHM_REVISION,
    analyze_ppm_near_black,
    parse_ppm,
)
from shared.vision.vm_console_observation import (
    DISPLAY_STATE_VOCABULARY,
    VmConsoleObservation,
    unavailable_observation,
)


def _intent(**args) -> SignalIntent:
    return SignalIntent(resolver_id="vm_console", tool="qkv_vm_console", args=args)


# ─── VmConsoleResolver ────────────────────────────────────────────────


def test_resolver_compiles_placeholder_targets_into_capture_intent():
    plan = VmConsoleResolver().compile(_intent(host="{{HOST}}", vm_id="{{VM_ID}}"))

    assert plan.status is ResolutionStatus.COMPILED
    assert plan.argv_template == []  # 刻意不产出命令字符串
    capture = plan.canonical_args["capture_intent"]
    assert capture["operation"] == "capture_baseline"
    assert capture["artifact_policy"] == "vm_console_v1"


def test_resolver_resolve_substitutes_variables_and_verifies():
    resolver = VmConsoleResolver()
    plan = resolver.compile(_intent(host="{{HOST}}", vm_id="{{VM_ID}}"))
    acquisition = resolver.resolve(plan, {"variables": {"HOST": "SVR_aCloud_668", "VM_ID": "123"}})

    assert acquisition.status is ResolutionStatus.VERIFIED
    assert acquisition.argv == [] and acquisition.command is None
    intent = capture_intent_from(acquisition)
    assert intent is not None
    assert intent.host_ref == "SVR_aCloud_668"
    assert intent.vm_ref == "123"


def test_resolver_unresolved_variables_needs_probe_fail_closed():
    resolver = VmConsoleResolver()
    plan = resolver.compile(_intent(host="{{HOST}}", vm_id="{{VM_ID}}"))
    acquisition = resolver.resolve(plan, {"variables": {}})

    assert acquisition.status is ResolutionStatus.NEEDS_PROBE
    assert any(issue.code == "VARIABLE_UNRESOLVED" for issue in acquisition.issues)


def test_resolver_blocks_unsafe_resolved_vm_id():
    resolver = VmConsoleResolver()
    plan = resolver.compile(_intent(host="{{HOST}}", vm_id="{{VM_ID}}"))
    acquisition = resolver.resolve(plan, {"variables": {"HOST": "node01", "VM_ID": "vm-web-01"}})

    assert acquisition.status is ResolutionStatus.BLOCKED
    assert any(issue.code == "VM_CONSOLE_VM_ID_INVALID" for issue in acquisition.issues)


@pytest.mark.parametrize(
    "args",
    [
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "command": "acli vm console"},
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "monitor_command": "sendkey up"},
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "path": "/sf/data/local/x.ppm"},
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "key": "down"},
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "shell": "sh"},
    ],
)
def test_resolver_rejects_free_form_execution_arguments(args):
    plan = VmConsoleResolver().compile(_intent(**args))

    assert plan.status is ResolutionStatus.BLOCKED
    assert any(issue.code == "VM_CONSOLE_ARGS_FORBIDDEN" for issue in plan.issues)


def test_resolver_rejects_unknown_capture_mode_and_bad_timeout():
    plan = VmConsoleResolver().compile(
        _intent(host="{{HOST}}", vm_id="{{VM_ID}}", capture_mode="wake_first")
    )
    assert plan.status is ResolutionStatus.BLOCKED

    plan = VmConsoleResolver().compile(_intent(host="{{HOST}}", vm_id="{{VM_ID}}", timeout=300))
    assert plan.status is ResolutionStatus.BLOCKED
    assert any(issue.code == "VM_CONSOLE_TIMEOUT_INVALID" for issue in plan.issues)


def test_capture_intent_rejects_unknown_operation():
    assert frozenset({"capture_baseline", "wake_down_key"}) == VM_CONSOLE_OPERATIONS
    with pytest.raises(ValidationError):
        VmConsoleCaptureIntent(operation="sendkey_up", host_ref="node01", vm_ref="123")


def test_wake_intent_builder_uses_fixed_operation():
    intent = build_wake_intent("node01", "123")

    assert intent.operation == "wake_down_key"
    assert intent.artifact_policy == "vm_console_v1"


def test_capture_intent_from_rejects_tampered_evidence():
    resolver = VmConsoleResolver()
    plan = resolver.compile(_intent(host="{{HOST}}", vm_id="{{VM_ID}}"))
    acquisition = resolver.resolve(plan, {"variables": {"HOST": "node01", "VM_ID": "123"}})

    assert capture_intent_from(acquisition) is not None
    tampered = acquisition.model_copy(update={"evidence": {"capture_intent": {"operation": "rm"}}})
    assert capture_intent_from(tampered) is None


# ─── 近黑检测 ─────────────────────────────────────────────────────────


def _ppm(width: int, height: int, pixel: tuple[int, int, int]) -> bytes:
    """构造纯色 P6 PPM。"""

    header = f"P6\n{width} {height}\n255\n".encode()
    return header + bytes(pixel) * (width * height)


def test_pure_black_frame_is_near_black():
    result = analyze_ppm_near_black(_ppm(64, 48, (0, 0, 0)))

    assert result["parse_ok"] is True
    assert result["algorithm_revision"] == NEAR_BLACK_ALGORITHM_REVISION
    assert result["near_black"] is True
    metrics = result["metrics"]
    assert metrics["mean_luma"] == 0.0
    assert metrics["non_black_ratio"] == 0.0
    assert metrics["ocr_available"] is False


def test_bright_console_frame_is_not_near_black():
    # 模拟有内容的控制台：白底 + 若干深色"文字"条带。
    width, height = 64, 48
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            if (y // 4) % 2 == 0 and 8 <= x < 56:
                pixels += bytes((240, 240, 240))
            else:
                pixels += bytes((20, 20, 20))
    result = analyze_ppm_near_black(bytes(f"P6\n{width} {height}\n255\n", "ascii") + bytes(pixels))

    assert result["parse_ok"] is True
    assert result["near_black"] is False


def test_malformed_ppm_fails_closed_without_near_black_claim():
    result = analyze_ppm_near_black(b"P5\n64 48\n255\n" + b"\x00" * 16)

    assert result["parse_ok"] is False
    assert result["near_black"] is False
    assert "P6" in result["parse_error"]


def test_ppm_size_limit_enforced():
    result = analyze_ppm_near_black(b"P6\n1 1\n255\n" + b"\x00" * (17 * 1024 * 1024))

    assert result["parse_ok"] is False
    assert result["near_black"] is False


def test_parse_ppm_header_comment_is_tolerated():
    image = parse_ppm(b"P6\n# screendump\n2 2\n255\n" + bytes((10, 10, 10)) * 4)

    assert image.width == 2 and image.height == 2


# ─── 视觉观察 Schema ──────────────────────────────────────────────────


def test_observation_requires_artifact_id_and_coerces_unknown_state():
    observation = VmConsoleObservation(
        display_state="weird_state",
        summary="控制台显示未知画面",
        confidence=0.91,
        artifact_id="artifact-001",
    )

    assert observation.display_state == "unknown"
    variables = observation.to_produced_variables()
    assert variables["VM_CONSOLE_STATE"] == "unknown"
    assert variables["VM_CONSOLE_ARTIFACT_ID"] == "artifact-001"


def test_observation_rejects_empty_artifact_id():
    with pytest.raises(ValidationError):
        VmConsoleObservation(display_state="kernel_panic", artifact_id="  ")


def test_observation_vocabulary_matches_design_document():
    assert frozenset(
        {
            "booting",
            "login_prompt",
            "desktop",
            "black_screen",
            "kernel_panic",
            "bsod",
            "installer_error",
            "application_error",
            "no_signal",
            "unknown",
        }
    ) == DISPLAY_STATE_VOCABULARY


def test_png_derive_isolated_enforces_limits_and_produces_png():
    from shared.vision.png_derive import derive_png_isolated

    png, width, height = derive_png_isolated(_ppm(16, 16, (30, 60, 90)))
    assert png.startswith(b"\x89PNG")
    assert (width, height) == (16, 16)

    # 超限 fail-closed
    import pytest as _pytest

    with _pytest.raises(ValueError):
        derive_png_isolated(_ppm(16, 16, (0, 0, 0)), max_side=8)
    # 非法输入 fail-closed
    with _pytest.raises(ValueError):
        derive_png_isolated(b"not-a-ppm")


def test_unavailable_observation_never_claims_no_fault():
    observation = unavailable_observation("artifact-002")

    assert observation.observation_status == "unavailable"
    assert observation.display_state == "unknown"
    assert observation.confidence == 0.0
    assert observation.needs_human_review is True


# ─── 回放 Fixture（§7.2）─────────────────────────────────────────────


def test_replay_fixtures_cover_five_scenarios_with_consistent_near_black():
    from shared.vision.replay_fixtures import run_replay

    results = run_replay()
    names = [item["name"] for item in results]
    assert names == ["normal", "black_screen", "kernel_panic", "bsod", "unavailable"]
    for item in results:
        assert item["near_black_matches_expectation"] is True, item
    # 黑屏场景必须命中近黑；正常/panic/蓝屏必须不命中。
    by_name = {item["name"]: item for item in results}
    assert by_name["black_screen"]["near_black"] is True
    assert by_name["normal"]["near_black"] is False
    assert by_name["kernel_panic"]["png_derived"] is True
    assert by_name["unavailable"]["parse_ok"] is False
    assert all(item["observation_contract_ok"] for item in results)
