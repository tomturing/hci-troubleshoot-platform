"""
tests/ conftest — 此目录下的测试主要针对 case-service
"""

import os
import sys


def _activate_service(service_name: str) -> None:
    """将指定服务路径加入 sys.path 首位，仅在 app 指向错误服务时清除重载"""
    svc_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "backend", service_name)
    )
    expect = os.path.normpath(os.path.join(svc_root, "app"))
    actual = os.path.normpath(
        getattr(sys.modules.get("app"), "__path__", [""])[0]
    ) if "app" in sys.modules else ""
    if expect != actual:
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                del sys.modules[key]
        if svc_root in sys.path:
            sys.path.remove(svc_root)
        sys.path.insert(0, svc_root)


_activate_service("case-service")
