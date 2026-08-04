import pytest
from shared.schemas.acquirer_args import validate_acquire_args
from shared.schemas.log_source_catalog import (
    normalize_log_path,
    resolve_log_source,
    validate_absolute_log_time,
)


def test_catalog_resolves_whitebox_and_blackbox_defaults():
    vtp = resolve_log_source("sfvt_vtpdaemon.log")
    assert vtp["family"] == "whitebox"
    assert vtp["path"] == "/sf/log"
    assert vtp["date_subpath"] == "vt"
    assert vtp["parser"] == "timestamped_lines"

    counter = resolve_log_source("LOG_ethtool_statistic.txt")
    assert counter["family"] == "vn_blackbox"
    assert counter["path"] == "/sf/log/vn-blackbox/today"
    assert "trend" in counter["predicates"]


def test_catalog_explicit_family_resolves_ambiguous_ifconfig():
    host = resolve_log_source("LOG_ifconfig.txt")
    vn = resolve_log_source("LOG_ifconfig.txt", source_family="vn_blackbox")

    assert host["path"] == "/sf/log/blackbox/today"
    assert vn["family"] == "vn_blackbox"
    assert vn["path"] == "/sf/log/vn-blackbox/today"


@pytest.mark.parametrize(
    "value",
    ["2026-07-30", "2026-07-30 00", "2026-07-30 00:10:20", "2026-07-30T00:10:20", "{{END}}"],
)
def test_absolute_log_time_contract_accepts_acli_shapes(value):
    assert validate_absolute_log_time(value) == (True, None)


@pytest.mark.parametrize(
    "value",
    ["now", "-1h", "最近一小时", "2026/07/30", "<日期>", "2026-13-40", "2026-02-29"],
)
def test_absolute_log_time_contract_rejects_relative_or_human_placeholders(value):
    ok, error = validate_absolute_log_time(value)
    assert not ok
    assert "绝对" in (error or "")


def test_path_contract_is_segment_bounded_and_canonical():
    assert normalize_log_path("/sf/log/today/") == "/sf/log/today"
    assert normalize_log_path("/sf/data/local/request/") == "/sf/data/local/request"
    for invalid in ("/sf/data/customer", "/sf/logs/today", "/sf/log/../cfg", "/sf/log/<日期>"):
        with pytest.raises(ValueError):
            normalize_log_path(invalid)


def test_data_local_is_request_id_auxiliary_scope_not_log_family():
    ok, error = validate_acquire_args("qfk_log", {"path": "/sf/data/local", "request_id": "abc"})
    assert ok, error

    ok, error = validate_acquire_args(
        "qfk_log",
        {"file": "task", "path": "/sf/data/local", "resource_keyword": "失败"},
    )
    assert not ok
    assert "不是日志目录" in (error or "")

    ok, error = validate_acquire_args(
        "qfk_log",
        {"path": "/sf/data/local", "request_id": "abc", "source_family": "whitebox"},
    )
    assert not ok
    assert "不得声明日志 source_family" in (error or "")


def test_bmc_is_explicit_capability_gap_not_local_file():
    ok, error = validate_acquire_args("qfk_log", {"file": "BMC_Event_Log"})
    assert not ok
    assert "不能由本机 qfk_log 获取" in (error or "")


def test_archive_search_requires_precheck():
    ok, error = validate_acquire_args(
        "qfk_log",
        {"file": "kernel.log", "include_archives": True},
    )
    assert not ok
    assert "archive_precheck=verified" in (error or "")

    ok, error = validate_acquire_args(
        "qfk_log",
        {"file": "kernel.log", "include_archives": True, "archive_precheck": "verified"},
    )
    assert ok, error


@pytest.mark.parametrize("container", ["asv", "anet", "host"])
def test_qfk_service_accepts_real_acli_service_groups(container):
    ok, error = validate_acquire_args(
        "qfk_service",
        {"resource_keyword": "redis", "container": container, "host": "{{HOST}}"},
    )
    assert ok, error


def test_qfk_service_rejects_terminal_container_name_as_service_group():
    ok, error = validate_acquire_args(
        "qfk_service",
        {"resource_keyword": "redis", "container": "dsv"},
    )
    assert not ok
    assert "container 非法" in (error or "")
