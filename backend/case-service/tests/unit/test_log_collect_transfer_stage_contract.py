"""三端一致性门禁：transfer_stage 枚举必须在契约、接收端、数据库约束中完全一致。

## 背景（2026-11-25 Q2026112500592）

#1648 在 bridge 生产端（`transferStageFor`）与展示契约新增了 `waiting` 阶段，
但漏改两处接收侧：

- `app/schemas/log_collect_evidence.py` 的 `Stage` Literal 只允许
  fetching/uploading/relaying；
- `database/desired_schema.sql` 的 `ck_lctm_stage` 同样只有三值。

结果 bridge 每条 waiting 样本都被 422 拒绝（本单 1300 条），平台
`detail.transfer` 快照永远缺失，面板把"还没开工"显示成"上传中 0/N 片"，
同时遮蔽了真正的 SSH 会话失联。

## 这个测试守什么

`waiting` 这类枚举值有三个独立登记点，任何一处漏改都会静默失败：

1. 展示契约 `contracts/log_collect_display.json` 的 `transfer_stage`；
2. 接收端 Pydantic `Stage`；
3. 数据库 CHECK 约束 `ck_lctm_stage`。

三者必须集合相等。生产端（Go `transferStageFor`）由 Go 单测锁死。
新增枚举时，这个测试会先失败，逼迫改动方把三端一起补齐。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

from app.schemas.log_collect_evidence import Stage, TransferMetricIn

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = REPO_ROOT / "contracts" / "log_collect_display.json"
DESIRED_SCHEMA = REPO_ROOT / "database" / "desired_schema.sql"

_CK_STAGE_RE = re.compile(
    r"ck_lctm_stage\s+CHECK\s*\(\(stage\)::text\s*=\s*ANY\s*\(\(ARRAY\[(.*?)\]\)::text\[\]\)\)",
    re.S,
)
_PG_VALUE_RE = re.compile(r"'([^']+)'::character varying")


def _contract_stages() -> set[str]:
    """展示契约里声明的 transfer_stage 枚举。"""
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return set(data["transfer_stage"].keys())


def _receiving_stages() -> set[str]:
    """bridge → 平台上报契约（Pydantic Literal）接受的 stage。"""
    return set(get_args(Stage))


def _db_stages() -> set[str]:
    """数据库 CHECK 约束允许的 stage。"""
    text = DESIRED_SCHEMA.read_text(encoding="utf-8")
    match = _CK_STAGE_RE.search(text)
    assert match is not None, "desired_schema.sql 中未找到 ck_lctm_stage 约束"
    return set(_PG_VALUE_RE.findall(match.group(1)))


def test_transfer_stage_three_way_aligned() -> None:
    contract = _contract_stages()
    receiving = _receiving_stages()
    db = _db_stages()
    assert contract == receiving == db, (
        "transfer_stage 三端不一致（新增枚举必须同时改契约、接收端与 DB 约束）："
        f" contract={sorted(contract)} receiving={sorted(receiving)} db={sorted(db)}"
    )


def test_waiting_stage_is_accepted_end_to_end() -> None:
    """waiting 是本工单的直接触发点，必须能被接收端模型接受。"""
    metric = TransferMetricIn(seq=0, stage="waiting", bytes_done=0)
    assert metric.stage == "waiting"
    assert "waiting" in _db_stages()
    assert "waiting" in _contract_stages()
