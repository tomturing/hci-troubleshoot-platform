"""QKV 解析兼容入口；在线、离线与试运行共用无服务依赖的解析实现。"""

# ruff: noqa: F401

from shared.signals.qkv_parser import (
    _convert_timestamp,
    _extract_by_produces,
    _extract_from_dialog_log,
    _extract_from_effect_verdict,
    _extract_from_vm_console_observation,
    _extract_hardcoded,
    _extract_time_from_log_line,
    _normalize_request_id,
    first_complete_produced_record,
    parse_frontend_value,
)
