"""场景语义展示：modules 不等于「用户选了什么」。

为什么单独锁这条：自定义场景（spc）下发时 acli 必须 `-m core`，平台会把
``payload.modules`` 覆写成 ``["core"]``。展示层如果直接读 modules，就把技术槽位
当成了用户选择——Q2026110302166 用户选的是自定义场景 test，页面写的是 core文件。
"""

from __future__ import annotations

from datetime import date

from app.routes.log_collect import resolve_scenario_display
from app.schemas.log_collect import LogCollectTaskCreate
from app.services.log_collect_service import _build_task_detail


def make_payload(**overrides) -> LogCollectTaskCreate:
    data = {
        "case_id": "Q2026110302166",
        "origin_node_ip": "172.28.24.1",
        "modules": ["core"],
        "date_from": date(2026, 11, 18),
        "date_to": date(2026, 11, 18),
    }
    data.update(overrides)
    return LogCollectTaskCreate(**data)


class TestScenarioSnapshot:
    def test_snapshot_is_persisted_into_detail(self):
        detail = _build_task_detail(
            make_payload(),
            scenario_snapshot={
                "kind": "custom",
                "id": "61f383e7-0000-0000-0000-000000000001",
                "revision": 3,
                "code": "test",
                "label": "test",
                "source": "spc",
                "item_count": 12,
            },
        )

        assert detail["scenario"]["kind"] == "custom"
        assert detail["scenario"]["label"] == "test"
        assert detail["scenario"]["source"] == "spc"

    def test_no_snapshot_keeps_detail_clean(self):
        detail = _build_task_detail(make_payload())

        assert "scenario" not in detail


class TestScenarioDisplay:
    def test_custom_spc_scenario_exposes_label_and_slot(self):
        kind, label, slot = resolve_scenario_display(
            {"detail": {"scenario": {"kind": "custom", "label": "test", "source": "spc"}}}
        )

        assert kind == "custom"
        assert label == "test"
        # core 只是 acli 槽位事实，必须与「用户选的场景」分开暴露。
        assert slot == "core"

    def test_custom_config_scenario_has_no_slot(self):
        kind, label, slot = resolve_scenario_display(
            {"detail": {"scenario": {"kind": "custom", "label": "cfg", "source": "visual"}}}
        )

        assert kind == "custom"
        assert slot is None

    def test_history_without_snapshot_falls_back_to_default(self):
        # 老任务没有快照：不能猜成自定义场景，否则会把 core 显示成某个场景名。
        assert resolve_scenario_display({"detail": {}}) == ("default", None, None)
        assert resolve_scenario_display({}) == ("default", None, None)

    def test_orm_like_row_is_supported(self):
        class Row:
            detail = {"scenario": {"kind": "custom", "label": "test", "source": "spc"}}

        assert resolve_scenario_display(Row()) == ("custom", "test", "core")
