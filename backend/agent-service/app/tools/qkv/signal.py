"""
QKV 前端信号数据模型与类型定义
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class FrontendQueryType(StrEnum):
    """前端信号查询类型"""

    ALERT = "alert"    # 告警信息
    TASK = "task"      # 操作任务
    DIALOG = "dialog"  # 对话/弹框日志


# ─── 关键词清洗后缀映射 ───────────────────────────────────────────────────────
# 用于规范化 -k 参数值，避免 LLM 抽取时混入类型后缀
_KEYWORD_CLEAN_SUFFIXES: dict[str, str] = {
    "alert": "告警",
    "task": "失败",
    "dialog": "弹框",
}


def _clean_keyword(keyword: str, query_type: str) -> tuple[str, bool]:
    """清洗关键词，去掉类型后缀，并检测是否包含状态标识。

    Args:
        keyword: 原始关键词
        query_type: 查询类型 (alert/task/dialog)

    Returns:
        (cleaned_keyword, is_failed)
        - cleaned_keyword: 清洗后的关键词
        - is_failed: 是否包含"失败"状态（仅对 task 有效）
    """
    if not keyword:
        return keyword, False

    suffix = _KEYWORD_CLEAN_SUFFIXES.get(query_type, "")
    is_failed = False
    cleaned = keyword

    # task 特殊处理：检测"失败"并设置状态
    if query_type == "task":
        if "失败" in cleaned:
            is_failed = True
            # 去掉"失败"（无论在哪个位置）
            cleaned = cleaned.replace("失败", "")
        # 额外清洗"成功"、"运行中"等状态后缀
        for status_suffix in ["成功", "运行中"]:
            cleaned = cleaned.replace(status_suffix, "")

    # 清洗类型后缀
    if suffix and cleaned.endswith(suffix):
        cleaned = cleaned[:-len(suffix)]

    return cleaned.strip(), is_failed


class FrontendSignal(BaseModel):
    """
    前端信号模型（QKV 加载处理）
    """

    query: FrontendQueryType = Field(..., description="Q: 查什么，告警/任务/弹框")
    keyword: str = Field(..., description="K: 匹配关键字")
    is_failed: bool = Field(default=False, description="是否只查失败任务 (仅在 query 为 task 时生效)")
    limit: int = Field(default=100, description="最大返回数据量限制")
    produces: list[dict[str, str]] = Field(
        default_factory=list,
        description="产出变量规格：[{name: 'HOST', path: 'host'}, ...]，为空时 parser 走硬编码兜底",
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrontendSignal:
        """从字典构建并校验，自动清洗关键词和检测状态。"""
        # 自动清洗关键词和检测状态
        keyword = data.get("keyword", "")
        query_type = data.get("query", "")
        if keyword and query_type:
            cleaned_keyword, detected_failed = _clean_keyword(keyword, query_type)
            data = {**data, "keyword": cleaned_keyword}
            # 如果检测到"失败"，设置 is_failed（除非显式指定了 is_failed=False）
            if detected_failed and data.get("is_failed") is None:
                data["is_failed"] = True
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> FrontendSignal:
        """从 JSON 串反序列化并校验"""
        data = json.loads(json_str)
        return cls.from_dict(data)
