"""日志任务响应模型的 INET 序列化契约。

回归背景：`origin_node_ip` 是 PostgreSQL INET 列，asyncpg 返回 `IPv4Address`
对象，而响应模型声明为 `str`。缺转换时 FastAPI 抛 `ResponseValidationError`
（500），staging 实测"创建任务 500、resolve 正常"。
"""

from datetime import UTC, date, datetime
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.schemas.log_collect import LogCollectTaskResponse


def make_task(origin_node_ip) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        task_id=uuid4(),
        case_id="Q2026101401684",
        origin_node_ip=origin_node_ip,
        modules=["vm_suspend"],
        date_from=date(2026, 10, 21),
        date_to=date(2026, 10, 22),
        ips=["all"],
        status="collecting",
        progress=42,
        created_by="client-a",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (IPv4Address("10.0.0.1"), "10.0.0.1"),
        (IPv6Address("2001:db8::1"), "2001:db8::1"),
        ("10.0.0.2", "10.0.0.2"),
    ],
)
def test_origin_node_ip_serialized_as_string(raw, expected):
    resp = LogCollectTaskResponse.model_validate(make_task(raw))

    assert resp.origin_node_ip == expected
    assert isinstance(resp.origin_node_ip, str)
