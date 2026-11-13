"""日志收集展示契约的单一来源校验。

目的：状态文案、体积格式化、阈值一旦前后端各写一份必然漂移
（历史缺陷：同一个 608 字节包出现 608 B / 0.0 MB / 608 字节三种口径）。
这里用「契约 JSON + 生成物 + CI 校验」把口径锁死。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from app.domain.log_collect_display_generated import (
    ARTIFACT_QUALITY_LABELS,
    CONCLUSION_LABELS,
    CONCLUSION_TONES,
    CONDITION_LABELS,
    PHASE_LABELS,
    THRESHOLDS,
    UPLOAD_MODE_LABELS,
    format_size,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = REPO_ROOT / "contracts" / "log_collect_display.json"


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_generated_files_match_contract() -> None:
    """生成物与契约必须逐字一致；漂移时提示重新生成。"""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_log_collect_display.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_labels_cover_every_enum_value() -> None:
    contract = _contract()
    assert set(CONCLUSION_LABELS) == set(contract["conclusion"])
    assert set(CONCLUSION_TONES) == set(contract["conclusion_tone"])
    assert set(PHASE_LABELS) == set(contract["phase"])
    assert set(CONDITION_LABELS) == set(contract["condition"])
    assert set(ARTIFACT_QUALITY_LABELS) == set(contract["artifact_quality"])
    assert set(UPLOAD_MODE_LABELS) == set(contract["upload_mode"])
    for labels in (
        CONCLUSION_LABELS,
        PHASE_LABELS,
        CONDITION_LABELS,
        ARTIFACT_QUALITY_LABELS,
        UPLOAD_MODE_LABELS,
    ):
        assert all(str(value).strip() for value in labels.values())


def test_thresholds_are_positive() -> None:
    for key, value in THRESHOLDS.items():
        assert isinstance(value, int) and value > 0, key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (0, "0 B"),
        (608, "608 B"),
        (1023, "1023 B"),
        (1024, "1.00 KiB"),
        (1536 * 1024, "1.50 MiB"),
        (989 * 1024 * 1024, "989 MiB"),
        (150 * 1024 * 1024, "150 MiB"),
    ],
)
def test_format_size_is_single_source_of_truth(value: int | None, expected: str) -> None:
    assert format_size(value) == expected


def test_small_archive_never_renders_as_zero_megabytes() -> None:
    """608 字节必须显示为 608 B；这正是历史缺陷的复现点。"""
    assert format_size(608) == "608 B"
    assert "0.0 MB" not in format_size(608)


def test_fault_catalog_is_actionable_and_safe() -> None:
    """错误字典必须可定界、可操作；界面绝不出现删除命令（误删事故防线）。"""
    from app.domain.log_collect_display_generated import (
        COMPONENTS,
        FAULT_ATTRIBUTION,
        FAULT_CATALOG,
        format_fault_text,
    )

    assert len(FAULT_CATALOG) >= 17
    for code, item in FAULT_CATALOG.items():
        assert item["component"] in COMPONENTS, code
        assert item["stage"] in FAULT_ATTRIBUTION["stage_order"], code
        assert item["problem"] and item["action"], code
        text = format_fault_text(code)
        assert text == f"{COMPONENTS[item['component']]['label']}：{item['problem']}；{item['action']}"
        # 只拦「命令」：像「不要自行删除文件」这类劝阻语是安全提示，属于防线本身。
        for forbidden in ("rm -", "rm /", "sudo rm", "del /", "rmdir", "rmtree", "请删除", "执行删除"):
            assert forbidden not in text, (code, forbidden)


def test_fault_detail_fields_match_contract() -> None:
    """归因字段与契约逐字对齐，且唯一调用链可透传。"""
    from app.domain.log_collect_display_generated import FAULT_ATTRIBUTION, fault_detail

    detail = fault_detail("LC-HCI-001", trace_id="trace-1")
    assert set(detail) == set(FAULT_ATTRIBUTION["fields"])
    assert detail["customer_text"] == "HCI环境：SSH连接失败；请尝试其他终端连接HCI验证连接是否正常"
    assert detail["scope_label"] == "内网"
    assert detail["trace_id"] == "trace-1"
