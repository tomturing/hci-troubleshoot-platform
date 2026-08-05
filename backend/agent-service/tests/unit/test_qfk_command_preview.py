from app.routes.capabilities import compile_qfk_command_preview


def test_command_preview_reuses_system_handler_and_keeps_runtime_variables():
    preview = compile_qfk_command_preview(
        {
            "id": "third_party_process",
            "acquire": {
                "tool": "qfk_system",
                "args": {
                    "command": "ps",
                    "command_args": ["-p", "{{PID}}", "-o", "cmd="],
                    "host": "{{HOST}}",
                    "container": "asv-con",
                    "cluster": True,
                    "timeout": 120,
                },
            },
            "match": {"type": "keyword", "pattern": "ClwDRDBClient", "mode": "or"},
        }
    )

    assert preview["tool"] == "qfk_system"
    assert preview["command"] == "acli --cluster --timeout 120 --container asv-con system ps -p '{{PID}}' -o cmd="
    assert preview["host"] == "{{HOST}}"
    assert preview["variables"] == ["HOST", "PID"]


def test_command_preview_uses_60_seconds_when_signal_omits_timeout():
    preview = compile_qfk_command_preview(
        {
            "acquire": {"tool": "qfk_system", "args": {"command": "df"}},
            "match": {"type": "keyword", "pattern": "data", "mode": "or"},
        }
    )

    assert preview["command"] == "acli --timeout 60 system df"


def test_command_preview_uses_service_runtime_mapping():
    preview = compile_qfk_command_preview(
        {
            "acquire": {
                "tool": "qfk_service",
                "args": {"resource_keyword": "asv", "container": "asv", "command": "status"},
            },
            "match": {"type": "keyword", "pattern": "running", "mode": "or"},
        }
    )

    assert preview["command"] == "acli service asv asv status"


def test_command_preview_uses_log_handler_and_preserves_time_template():
    preview = compile_qfk_command_preview(
        {
            "acquire": {
                "tool": "qfk_log",
                "args": {
                    "file": "sfvt_vtpdaemon.log",
                    "host": "{{HOST}}",
                    "time_window": "{{END}}",
                },
            },
            "match": {"type": "keyword", "pattern": "too many file", "mode": "or"},
        }
    )

    assert preview["command"] == (
        "acli log get -E -k 'too\\ many\\ file' "
        "-f sfvt_vtpdaemon.log -p /sf/log -t '{{END}}'"
    )
    assert preview["variables"] == ["END", "HOST"]
