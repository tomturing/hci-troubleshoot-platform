"""CDD 共享代码唯一来源门禁。"""

from pathlib import Path

AGENT_SERVICE_ROOT = Path(__file__).resolve().parents[2]
LEGACY_CDD_PATH = AGENT_SERVICE_ROOT / "app" / "adapters" / "agents" / "htp" / "cdd"
LEGACY_IMPORT_PREFIX = "app.adapters.agents.htp.cdd"


def test_legacy_cdd_compatibility_package_is_removed():
    """Agent Service 不得保留会掩盖根共享实现的 CDD 转发包。"""

    assert not LEGACY_CDD_PATH.exists()


def test_agent_runtime_does_not_import_legacy_cdd_path():
    """运行时代码必须直接导入 shared.cdd。"""

    app_root = AGENT_SERVICE_ROOT / "app"
    offenders = [
        str(path.relative_to(AGENT_SERVICE_ROOT))
        for path in app_root.rglob("*.py")
        if LEGACY_IMPORT_PREFIX in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
