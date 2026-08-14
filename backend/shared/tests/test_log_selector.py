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
