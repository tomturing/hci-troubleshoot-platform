"""
项目根 conftest — 公共路径注册

uv 管理虚拟环境和第三方依赖，此文件仅注册 root 和 backend（shared 库）路径。
各微服务的 app/ 路径由对应服务的 tests/conftest.py 按需注册，
避免多个服务共享 app/ 命名空间导致 import 冲突。

运行方式: uv run pytest
"""

import sys
import os

_root = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.join(_root, "backend")

# 仅注册公共路径：项目根（tests/）和 backend（shared 库）
for p in (_root, _backend):
    if p not in sys.path:
        sys.path.insert(0, p)
