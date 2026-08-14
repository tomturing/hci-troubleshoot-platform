"""Diagnosis Service 测试路径注册。"""

import os
import sys

_service_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_expected_app = os.path.normpath(os.path.join(_service_root, "app"))
_actual_app = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""

if _expected_app != _actual_app:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    if _service_root in sys.path:
        sys.path.remove(_service_root)
    sys.path.insert(0, _service_root)
