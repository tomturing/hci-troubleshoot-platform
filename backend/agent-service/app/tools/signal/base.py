"""
关键信号抽象基类（KeySignal Base Class）

第一性原理：
- 所有排障信号均源自 KBD/SOP 的自然语言描述
- 基类定义通用提取、验证与执行接口
- 派生类实现具体的前端元数据提取或后端健康度判定逻辑
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SignalCategory(StrEnum):
    """
    关键信号类别（用于类型判别与路由）

    基于第一性原理，信号在排障流程中扮演两种截然不同的角色：
    - FRONTEND（前端信号）：故障现场元数据提取者，负责生产变量
    - BACKEND（后端信号）：运行时健康度判定者，负责消费变量
    """

    FRONTEND = "frontend"  # 前端信号：告警/任务/弹框 → 元数据提取
    BACKEND = "backend"    # 后端信号：日志/服务/系统 → 布尔判定


class KeySignal(BaseModel, ABC):
    """
    关键信号抽象基类

    核心职责：
    1. 作为 KBD/SOP 自然语言描述的结构化表示
    2. 定义信号的通用属性与行为接口
    3. 提供 LLM 提取的目标数据模型

    派生类：
    - FrontendSignal：前端信号，负责故障现场元数据提取（生产者）
    - BackendSignal：后端信号，负责运行时健康度判定（消费者）
    """

    signal_category: SignalCategory = Field(
        ...,
        description="信号类别：frontend（前端信号）或 backend（后端信号）"
    )
    keyword: str = Field(
        ...,
        description="K: 匹配关键字，从 KBD/SOP 文本中提取的核心检索词"
    )
    description: str | None = Field(
        default=None,
        description="对原始排障步骤的自然语言说明"
    )

    @abstractmethod
    def extract(self) -> dict[str, Any]:
        """
        从 KBD/SOP 文本中提取信号配置（子类实现）

        Returns:
            结构化的信号参数字典，供具体执行引擎使用
        """
        pass

    @abstractmethod
    def validate(self) -> tuple[bool, str | None]:
        """
        校验信号参数完整性（子类实现）

        Returns:
            (is_valid, error_message)
        """
        pass

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeySignal:
        """
        从字典构造信号实例（工厂方法）

        根据signal_category自动路由到对应的派生类构造器
        """
        from app.tools.signal.backend import BackendSignal
        from app.tools.signal.frontend import FrontendSignal

        category = data.get("signal_category")
        if category == SignalCategory.FRONTEND:
            return FrontendSignal.model_validate(data)
        elif category == SignalCategory.BACKEND:
            return BackendSignal.model_validate(data)
        else:
            raise ValueError(f"未知的信号类别: {category}")

    @classmethod
    def from_json(cls, json_str: str) -> KeySignal:
        """从 JSON 字符串反序列化信号实例"""
        import json
        data = json.loads(json_str)
        return cls.from_dict(data)


