"""KBD CLI 的仓库级运行时准备。

KBD 管道位于仓库根目录下的 ``data-pipeline/``，而日志审计复用同一仓库
``backend/shared`` 的 Agent 契约。源码检出方式运行时，Python 不会自动把
``backend/`` 放入模块搜索路径；若等到 Stage 6 再导入，会让前五个阶段完成后
才暴露环境错误。

本模块是这项边界的唯一实现：KBD 包导入时只准备本仓库的源码路径；需要共享
契约的 CLI 命令再通过 :func:`require_shared_contracts` 在执行前显式预检。
它不读取环境密钥、不连接数据库，也不改变任何业务数据。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def repository_root() -> Path:
    """返回由当前源码位置确定的仓库根目录。"""

    return Path(__file__).resolve().parents[2]


def bootstrap_repository_imports() -> Path:
    """让源码检出模式的 KBD 包可导入 ``backend/shared``。

    ``python -m data-pipeline.kbd.run`` 从仓库根目录运行时天然可以定位
    ``data-pipeline``，但不能定位顶级包 ``shared``。这里仅插入当前 checkout 的
    ``backend``，从而保证生产管道和 Agent 使用同一份版本化契约。

    Returns:
        已加入 ``sys.path`` 的 ``backend`` 目录。

    """

    backend_root = repository_root() / "backend"
    backend_path = str(backend_root)
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    return backend_root


def require_shared_contracts() -> None:
    """在有 Stage 6 的命令开始前验证 Agent 共享契约可导入。

    预检只检查 Python 模块解析；不连接数据库、不运行审计、不执行生产阶段。
    这样即使 checkout 损坏，也会在 fetch/import/Vision/LLM 调用前给出明确错误。
    """

    backend_root = bootstrap_repository_imports()
    if not (backend_root / "shared").is_dir():
        raise RuntimeError(
            "KBD 日志审计依赖的 Agent 共享契约不可用："
            f"未在当前源码仓库找到 {backend_root / 'shared'}。"
            "请从项目根目录运行，或使用包含 backend/shared 的完整 checkout。"
        )
    try:
        importlib.import_module("shared.resolution.review")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "KBD 日志审计依赖的 Agent 共享契约不可用："
            f"{exc.name or exc!s}。已尝试加载 {backend_root}；"
            "请执行 uv sync，并确认 backend/shared 未被删除。"
        ) from exc
