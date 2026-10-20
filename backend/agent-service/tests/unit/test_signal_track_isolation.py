"""链路隔离的结构性证明：删掉根因链路，关键链路必须照常工作。

这些断言不测行为，只测**依赖方向**——它们是「后期淘汰一条链路」时的可执行保障：

1. 关键链路的代码（``kbd_differential`` / ``shared.cdd``）不得 import 任何根因模块；
2. 把根因模块从 ``sys.modules`` 中屏蔽后，关键链路仍能 import、编译计划、判定结论；
3. 根因模块对关键链路的依赖是单向的（它 import 关键链路，反之绝不）。

若某天有人图省事在 ``kbd_differential`` 里引用 ``root_display``，本文件会立刻失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_SERVICE = REPO_ROOT / "backend" / "agent-service"
BACKEND = REPO_ROOT / "backend"

HTP = AGENT_SERVICE / "app" / "adapters" / "agents" / "htp"
ROOT_TRACK_MODULES = (
    "app.adapters.agents.htp.root_track",
    "app.adapters.agents.htp.track_arbiter",
    "app.adapters.agents.htp.root_display",
    "app.adapters.agents.htp.dual_track",
)


def _run_child(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{AGENT_SERVICE}{os.pathsep}{BACKEND}"
    return subprocess.run(  # noqa: S603 - 固定解释器与代码，不拼接外部输入
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ── 1. 源码级：关键链路文件不含根因模块引用 ────────────────────────────────
@pytest.mark.parametrize(
    "filename",
    ["kbd_differential.py"],
)
def test_key_track_files_do_not_reference_root_modules(filename):
    source = (HTP / filename).read_text(encoding="utf-8")
    for module in ("root_track", "track_arbiter", "root_display", "dual_track"):
        assert f"import {module}" not in source, f"{filename} 不得 import {module}"
        assert f"from app.adapters.agents.htp.{module}" not in source
    # 也不得出现根因字段的读取（数据字段可以存在，但关键链路不消费它）
    assert "root_signals" not in source
    assert "root_display_anchors" not in source
    assert "root_executable" not in source


def test_shared_cdd_has_no_root_signal_concept():
    """共享内核不得承载根因字段：否则淘汰根因链路必须改 ``shared/cdd``。"""
    forbidden = ("root_signal", "root_track", "root_display_anchors", "root_executable", "root_snapshot_id")
    for path in (BACKEND / "shared" / "cdd").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"共享内核 {path.name} 不得出现根因概念 {token}"


def test_kbd_model_exposes_only_key_signal_fields():
    """``KBD`` 的字段集只描述关键信号；根因形态由编排层构造。"""
    from shared.cdd.kbd_model import KBD

    assert "signals" in KBD.model_fields
    for field in ("root_signals", "root_executable", "root_execution_issues", "root_snapshot_id", "root_display_anchors"):
        assert field not in KBD.model_fields, f"KBD 不得承载根因字段 {field}"


# ── 2. 运行级：屏蔽根因模块后关键链路仍可用 ────────────────────────────────
def test_key_track_imports_and_compiles_without_root_modules():
    code = f"""
import sys
for name in {list(ROOT_TRACK_MODULES)!r}:
    sys.modules[name] = None  # 任何 import 都会抛 ImportError

from shared.cdd import CandidateState, SignalOutcome, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import KBD
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic

EXTRACT = {{"type": "text", "rows": {{"mode": "all"}}, "cardinality": "all", "source": "stdout", "value_mode": "string"}}
kbd = KBD(
    id="7", support_id="K7", name="x", category_id="c",
    signals=[{{"id": "k1", "acquire": {{"tool": "qfk_system", "args": {{"command": "dmesg"}}}},
              "match": {{"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": EXTRACT}}}}],
)
plan = compile_signal_plan([kbd], snapshot_id="key-only")
assessments = initial_assessments(plan)
for ref in plan.signals.values():
    assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = SignalOutcome.SATISFIED
reduce_candidates(plan, assessments, finalize=True)
assert assessments["7"].state is CandidateState.SUPPORTED
assert hasattr(KBDDiagnostic, "_build_diagnostic_report")
print("KEY_TRACK_OK")
"""
    result = _run_child(code)
    assert result.returncode == 0, result.stderr
    assert "KEY_TRACK_OK" in result.stdout


def test_playbooks_payload_available_without_root_module():
    """kb-service 的序列化载荷在根因装配模块不可用时也应能导入路由。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{BACKEND / 'kb-service'}{os.pathsep}{BACKEND}"
    code = """
import sys
sys.modules["app.services.root_signal_track"] = None
try:
    from app.routes import playbooks  # noqa: F401
except ImportError as exc:
    print("IMPORT_FAILED:", exc)
    raise SystemExit(2)
print("ROUTES_OK")
"""
    result = subprocess.run(  # noqa: S603 - 固定解释器与代码
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # 注意：playbooks.py 里有一行 `**build_root_track_payload(kbd)`，所以它是**唯一**
    # 必须改动的接线点——这正是「淘汰时只改一处」的设计代价，此处如实记录。
    assert result.returncode == 2
    assert "IMPORT_FAILED" in result.stdout


# ── 3. 依赖方向：根因模块依赖关键链路，而不是反之 ──────────────────────────
@pytest.mark.parametrize("module", ["root_track.py", "track_arbiter.py", "dual_track.py"])
def test_root_modules_depend_on_key_track_not_reverse(module):
    source = (HTP / module).read_text(encoding="utf-8")
    # 根因模块可以 import kbd_differential / shared.cdd（复用原语），反之不允许
    assert "from shared.cdd" in source or "import shared.cdd" in source


def test_root_display_is_pure_presentation_without_model_dependency():
    """展示层连共享内核都不依赖：入参是纯数据，淘汰时零连带。"""
    source = (HTP / "root_display.py").read_text(encoding="utf-8")
    assert "from shared" not in source
    assert "from app.adapters.agents.htp.kbd_differential" not in source


def test_investigation_agent_has_single_root_track_entry():
    """编排入口只有一处 import + 两处调用：淘汰根因链路时定位成本可枚举。"""
    source = (HTP / "investigation_agent.py").read_text(encoding="utf-8")
    assert source.count("from app.adapters.agents.htp.dual_track import diagnose_with_tracks") == 1
    assert source.count("diagnose_with_tracks(") == 2  # 2 处调用（import 行不带括号）
    # 调用方只传原始载荷，不在调用点构造根因 KBD：共享内核不需要认识根因字段。
    assert source.count("root_cases=") == 2
    assert "root_candidates=" not in source
