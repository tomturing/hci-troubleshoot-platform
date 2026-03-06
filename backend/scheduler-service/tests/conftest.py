"""
Scheduler Service 测试 conftest — 激活 scheduler-service 的 app 命名空间
"""

import os
import sys
from unittest.mock import MagicMock

# kubernetes 库加载极慢且测试无需真实 K8s API，在导入任何 app 代码前预先 mock。
# 注意：必须同时 mock 所有子模块，否则 Python 会在 sys.modules 未命中时走磁盘加载。
_k8s_mock = MagicMock()
for _mod in (
    "kubernetes",
    "kubernetes.client",
    "kubernetes.client.rest",
    "kubernetes.client.api",
    "kubernetes.client.models",
    "kubernetes.config",
    "kubernetes.watch",
    "kubernetes.stream",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = _k8s_mock

_svc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_expect = os.path.normpath(os.path.join(_svc_root, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    if _svc_root in sys.path:
        sys.path.remove(_svc_root)
    sys.path.insert(0, _svc_root)
