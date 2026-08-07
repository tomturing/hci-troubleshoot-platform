"""受限的现场能力探针契约。

Resolver 只依赖这个只读接口，不能自行打开 SSH、执行 Shell 或改变目标状态；具体的
Bridge/SSH 实现由调用层注入，便于在单元测试中替换为 fixture。
"""

from __future__ import annotations

from typing import Protocol


class CapabilityProbe(Protocol):
    """消费前允许使用的最小只读探针。"""

    def path_exists(self, absolute_path: str) -> bool: ...

    def command_supported(self, argv: list[str]) -> bool: ...
