"""在线和离线共用的日志粗筛选择器回归。"""

import pytest
from shared.resolution.log_selector import build_log_selector


@pytest.mark.parametrize("matcher_type", ["threshold", "delta", "trend"])
def test_numeric_ai_extract_uses_persisted_row_keywords_without_metric(matcher_type: str):
    """AI 数值抽取应从持久化 rows.include 构建有界粗筛。"""

    selector, extended, resolved_type = build_log_selector(
        matcher={
            "type": matcher_type,
            "extract": {
                "type": "text",
                "rows": {"mode": "keywords", "include": ["info block-jobs", "Completed"]},
                "ai_extract": {"instruction": "提取已完成和总数量"},
            },
        }
    )

    assert selector == "Completed|info\\ block\\-jobs"
    assert extended is True
    assert resolved_type == matcher_type


def test_numeric_ai_extract_falls_back_to_runtime_keywords_but_never_unbounded():
    """兼容旧运行路径，同时禁止 AI 配置成为整文件采集通行证。"""

    assert build_log_selector(
        matcher={
            "type": "threshold",
            "extract": {"ai_extract": {"instruction": "提取数值"}},
        },
        keywords=["disk usage"],
    ) == ("disk\\ usage", True, "threshold")

    with pytest.raises(ValueError, match="必须提供 metric"):
        build_log_selector(
            matcher={
                "type": "threshold",
                "extract": {"ai_extract": {"instruction": "提取数值"}},
            }
        )


def test_exists_matcher_with_persisted_row_include_pushes_down_selector():
    """exists 模式下如果包含持久化 rows.include，应下推为转义的正则粗筛。"""

    selector, extended, resolved_type = build_log_selector(
        matcher={
            "type": "exists",
            "extract": {
                "type": "text",
                "rows": {"mode": "keywords", "include": ["Get {{VM}} from vmlist", "can't open file"]},
            },
        }
    )

    assert selector == "Get\\ \\{\\{VM\\}\\}\\ from\\ vmlist|can't\\ open\\ file"
    assert extended is True
    assert resolved_type == "exists"


def test_selector_can_preserve_templates_for_bundle_compilation():
    """Bundle 编译阶段保留模板，待场景变量渲染后再完成正则字面量转义。"""

    selector, extended, resolved_type = build_log_selector(
        matcher={
            "type": "exists",
            "extract": {
                "type": "text",
                "rows": {"mode": "keywords", "include": ["Get {{VM}} from vmlist or conf failed"]},
            },
        },
        preserve_placeholders=True,
    )

    assert selector == r"Get\ {{VM}}\ from\ vmlist\ or\ conf\ failed"
    assert extended is True
    assert resolved_type == "exists"


def test_exists_matcher_without_include_falls_back_to_dot():
    """exists 模式下若无任何 include 或 filter_keywords，保持回退为 . 匹配。"""

    selector, extended, resolved_type = build_log_selector(
        matcher={
            "type": "exists",
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
            },
        }
    )

    assert selector == "."
    assert extended is True
    assert resolved_type == "exists"
