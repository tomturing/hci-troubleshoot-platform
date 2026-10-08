"""SRC-L3 工单归属审计脚本纯函数单测（无需数据库）。

直接按文件路径加载 scripts/verify/verify_case_ownership.py，验证 classify_client_id
的归属形态分类契约——该契约是运维巡检 / 发布门禁的判据基础，必须可脱离 DB 单测。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify" / "verify_case_ownership.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_case_ownership", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vco = _load_module()


def test_classify_null_is_abnormal():
    assert vco.classify_client_id(None) == "null"
    assert "null" in vco.ABNORMAL_CLASSIFICATIONS


def test_classify_empty_and_whitespace_is_abnormal():
    assert vco.classify_client_id("") == "empty"
    assert vco.classify_client_id("   ") == "empty"
    assert vco.classify_client_id("\t ") == "empty"
    assert "empty" in vco.ABNORMAL_CLASSIFICATIONS


def test_classify_placeholder_token_is_abnormal():
    # SRC-L2 已删除的占位 token 复发即桩鉴权回退，必须判异常
    assert vco.classify_client_id("client-session-placeholder-token") == "placeholder"
    assert "placeholder" in vco.ABNORMAL_CLASSIFICATIONS


def test_classify_random_client_shapes_are_legal():
    # 历史/前端旧形态 client-<随机>
    assert vco.classify_client_id("client-mpayv9r9-2cox3cx") == "anon_shape_ok"
    # 当前网关 Cookie 新形态：uuid4().hex（32-hex）
    assert vco.classify_client_id("a1b2c3d4e5f60718293a4b5c6d7e8f90") == "anon_shape_ok"
    # 测试种子形态
    assert vco.classify_client_id("client-test-001") == "anon_shape_ok"
    assert "anon_shape_ok" not in vco.ABNORMAL_CLASSIFICATIONS


def test_classify_system_and_seed_identities_are_ok():
    # 仿真系统身份 / 回归测试种子：归属键存在即合法，非孤儿
    assert vco.classify_client_id("hci-sim-admin") == "ok"
    assert vco.classify_client_id("regression-client") == "ok"
    assert "ok" not in vco.ABNORMAL_CLASSIFICATIONS


def test_self_test_passes_all_cases():
    # 脚本内置自检应零失败
    assert vco.self_test() == 0
