"""证据端点记录上报方 bridge 版本的单测（无需数据库）。

为什么要有这组测试：旧现场（Q2026112400942）平台侧答不出"这台跑的哪个版本"，
因为唯一来源 bridge.startup 走桥侧直传通道、一失败就永久丢失。修法是让每条证据
请求带 X-Bridge-Version 并落到任务 detail。这里钉住三件事：
1) 版本串白名单与长度上限（防止任意文本塞进 detail 污染界面）；
2) 非法/空版本不写库，避免把已记录的版本覆盖成空；
3) 三个高频端点确实声明了这个请求头（防止以后被误删）。
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest
from app.routes import log_collect_evidence as route

pytestmark = pytest.mark.unit


class _FakeSession:
    """替身 session：只记录 execute 调用，不连数据库。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(statement), params or {}))
        return None


class _FakeService:
    def __init__(self) -> None:
        self.repository = type("_Repo", (), {"session": _FakeSession()})()


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("v2.37.0-13-g7ea91e5d", "v2.37.0-13-g7ea91e5d"),
        ("  v2.16.0-dev  ", "v2.16.0-dev"),
        ("v2.37.0-9-gcef9e5fc", "v2.37.0-9-gcef9e5fc"),
        ("", ""),
        ("   ", ""),
        ("v2 37", ""),
        ("v2;rm -rf /", ""),
        ("v2.37\n0", ""),
        ("a" * 65, ""),
    ],
)
def test_clean_bridge_version_whitelist(raw: str, want: str) -> None:
    """只有版本串常见字符、且不超过 64 字符才被接受。"""
    assert route._clean_bridge_version(raw) == want


@pytest.mark.asyncio
async def test_record_bridge_version_writes_detail() -> None:
    """合法版本写进 detail.bridge_version，并且只在变化时写。"""
    service = _FakeService()
    task_id = uuid4()
    await route._record_bridge_version(service, task_id, "v2.37.0-13-g7ea91e5d")

    calls = service.repository.session.calls
    assert len(calls) == 1, "合法版本应当只有一次写入"
    sql, params = calls[0]
    assert "bridge_version" in sql
    assert "IS DISTINCT FROM" in sql, "必须带上 IS DISTINCT FROM，否则 metrics 每 2 秒一条会写放大"
    assert params["version"] == "v2.37.0-13-g7ea91e5d"
    assert params["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_record_bridge_version_skips_invalid() -> None:
    """非法或空版本不写库：宁可没有，也不能用脏值覆盖已记录版本。"""
    service = _FakeService()
    await route._record_bridge_version(service, uuid4(), "")
    await route._record_bridge_version(service, uuid4(), "not a version")
    await route._record_bridge_version(service, uuid4(), None)
    assert service.repository.session.calls == []


@pytest.mark.parametrize(
    "handler",
    [
        "put_metric",
        "put_network_probe",
        "put_artifact_manifest",
        "put_process_log",
        "put_evidence_error",
        "put_failure_snapshot",
    ],
)
def test_evidence_routes_declare_bridge_version_header(handler: str) -> None:
    """全部证据端点都必须声明 X-Bridge-Version。

    覆盖关系：metrics 覆盖 P2 交付全程，network-probe 覆盖每个任务的第一枪，
    artifact-manifest 覆盖没有 metrics 流的 P1 直传；进程日志/证据失败/失败快照
    是失败任务最后仍会触发的端点，缺一个就会让版本字段继续出现空洞。
    """
    func = getattr(route, handler)
    parameter = inspect.signature(func).parameters.get("x_bridge_version")
    assert parameter is not None, f"{handler} 缺少 x_bridge_version 参数"
    assert parameter.default.alias == route.BRIDGE_VERSION_HEADER
