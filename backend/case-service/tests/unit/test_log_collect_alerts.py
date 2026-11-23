"""采集告警抽取的对抗性单测：噪音不误报、真告警不漏报、合并幂等。"""

from __future__ import annotations

from app.domain.log_collect_alerts import (
    CODE_IO,
    CODE_PARTIAL,
    COLLECTION_LOG_SUFFIX,
    extract_collect_alerts,
    is_collect_alert_source,
    merge_collect_alerts,
    primary_collect_alert,
)

PATH = "/sf/log/today/acli/plugins/log_collect/collection_log.log"
TRACE = "lc-Q2026112300867-1795417871054"


def test_source_whitelist_only_collection_log():
    assert is_collect_alert_source(PATH) is True
    assert is_collect_alert_source("/sf/log/today/acli/acli.log") is False
    assert is_collect_alert_source("/sf/log/today/acli/cmd_output/dmesg.log") is False
    assert is_collect_alert_source("") is False


def test_eio_is_promoted_to_io_code_with_evidence():
    content = (
        "2026-11-23 15:12:04,150 - single_collect.py[line:265] - ERROR: "
        "failed add /sf/etc/halt.d/K25filebeat to tar, error: file: ... not exists\n"
        "2026-11-23 15:12:23,893 - single_collect.py[line:265] - ERROR: "
        "failed add /sf/etc/isv-guide/version1.0/uinfor/certificate.png to tar, "
        "error: [Errno 5] Input/output error\n"
    )
    alerts = extract_collect_alerts(PATH, content, trace_id=TRACE, node_ip="10.30.2.35")
    assert [a["code"] for a in alerts] == [CODE_PARTIAL, CODE_IO]
    eio = alerts[1]
    assert eio["file"] == "/sf/etc/isv-guide/version1.0/uinfor/certificate.png"
    assert "Errno 5" in eio["line"]
    assert eio["trace_id"] == TRACE and eio["node_ip"] == "10.30.2.35"
    assert primary_collect_alert({"collect_alerts": alerts})["code"] == CODE_IO


def test_benign_noise_is_ignored():
    content = "\n".join(
        [
            "ERROR: 2026/11/23 15:11:30 cmd.go:35: [SF_DCODE 0x000008042DFF0008] command failed: exit status 1",
            "WARNING: built_in_plugin.go:70: HCI not update, skipping initialization",
            "INFO: get_gpuinfo.py: run command ... code: 1, error: No such file or directory",
            "failed add /sf/etc/x to tar, error: permission denied",  # 非本规则覆盖的原因
        ]
    )
    assert extract_collect_alerts(PATH, content) == []


def test_non_collection_source_yields_no_alert_even_with_eio():
    content = "failed add /x to tar, error: [Errno 5] Input/output error"
    assert extract_collect_alerts("/sf/log/today/acli/acli.log", content) == []


def test_merge_replaces_same_source_and_dedups():
    old = [
        {"code": CODE_IO, "source": PATH, "file": "/stale.png", "line": "old"},
        {"code": CODE_PARTIAL, "source": PATH, "file": "/gone", "line": "old"},
    ]
    fresh = [{"code": CODE_IO, "source": PATH, "file": "/new.png", "line": "new"}]
    merged = merge_collect_alerts(old, fresh, source=PATH)
    assert len(merged) == 1
    assert merged[0]["file"] == "/new.png"


def test_merge_keeps_other_sources_and_is_bounded():
    other = {"code": CODE_PARTIAL, "source": "/other/collect.log", "file": "/a"}
    existing = [other] + [
        {"code": CODE_PARTIAL, "source": PATH, "file": f"/f{i}"} for i in range(30)
    ]
    merged = merge_collect_alerts(existing, [], source=PATH)
    assert other in merged
    assert len(merged) <= 20


def test_primary_is_none_without_alerts():
    assert primary_collect_alert({}) is None
    assert primary_collect_alert({"collect_alerts": "not-a-list"}) is None
    assert COLLECTION_LOG_SUFFIX.endswith("collection_log.log")
