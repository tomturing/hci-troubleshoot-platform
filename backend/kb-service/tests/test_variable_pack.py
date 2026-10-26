"""变量包合并规则单测。

变量包的目标是消灭"同一份通用变量被复制粘贴到多个 SOP"的漂移：
SOP#1/#2/#3 曾各存一份 22 个同名变量，一次整改要改 66 处。
"""

from __future__ import annotations

from app.services.variable_pack import expand_pack_variables

PACK_BASE = [
    {
        "name": "qemu_log_path",
        "type": "string",
        "acquisition_strategy": "path_template",
        "expression": "/sf/log/{task_day}/sfvt_qemu_{vmid}.log",
        "depends_on": ["task_day", "vmid"],
        "description": "QEMU 日志路径",
    },
    {
        "name": "mysql_status",
        "type": "string",
        "acquisition_strategy": "tool_call",
        "acquisition_tool": "acli_exec",
        "output_path": "stdout",
        "depends_on": [],
        "description": "MySQL 服务状态",
    },
]


def test_pack_variables_are_injected_in_order():
    merged, conflicts = expand_pack_variables(PACK_BASE, [])

    assert [item["name"] for item in merged] == ["qemu_log_path", "mysql_status"]
    assert conflicts == []


def test_own_definition_wins_over_pack():
    """SOP 自有定义优先，且冲突要显式返回，不能静默覆盖。"""
    own = [
        {
            "name": "mysql_status",
            "type": "string",
            "acquisition_strategy": "user_input",
            "depends_on": [],
            "description": "本 SOP 定制：手工确认 MySQL 状态",
        }
    ]
    merged, conflicts = expand_pack_variables(PACK_BASE, own)

    assert conflicts == ["mysql_status"]
    by_name = {item["name"]: item for item in merged}
    assert by_name["mysql_status"]["acquisition_strategy"] == "user_input"
    # 不能出现重复声明
    assert [item["name"] for item in merged].count("mysql_status") == 1


def test_deprecated_pack_variable_skipped():
    pack = [
        *PACK_BASE,
        {
            "name": "legacy_var",
            "type": "string",
            "acquisition_strategy": "user_input",
            "deprecated": True,
            "description": "历史遗留",
        },
    ]
    merged, _ = expand_pack_variables(pack, [])

    assert "legacy_var" not in {item["name"] for item in merged}


def test_invalid_pack_variable_name_skipped():
    """包里混入非法变量名（如中文）时跳过，绝不注入到 SOP。"""
    pack = [
        *PACK_BASE,
        {"name": "任务原始数据", "type": "string", "acquisition_strategy": "user_input"},
    ]
    merged, _ = expand_pack_variables(pack, [])

    assert "任务原始数据" not in {item["name"] for item in merged}


def test_own_variables_appended_after_pack():
    own = [
        {
            "name": "vmid",
            "type": "string",
            "acquisition_strategy": "skill:hci-task-parsing",
            "acquisition_tool": "hci-task-parsing",
            "output_path": "values.vm",
            "depends_on": [],
            "description": "虚拟机 ID",
        }
    ]
    merged, _ = expand_pack_variables(PACK_BASE, own)

    names = [item["name"] for item in merged]
    assert names[-1] == "vmid"
    assert set(names[:2]) == {"qemu_log_path", "mysql_status"}


def test_duplicate_names_inside_pack_deduped():
    pack = [PACK_BASE[0], dict(PACK_BASE[0])]
    merged, _ = expand_pack_variables(pack, [])

    assert [item["name"] for item in merged] == ["qemu_log_path"]
