"""从种子 SQL 的实际 V2 输出回归语义路由、在线 Matcher 与 CDD；不改数据库。"""

import importlib.util
import os
from pathlib import Path

import pytest
from shared.schemas.semantic_routing import resolve_candidates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_KB_POSTGRES_INTEGRATION") != "1", reason="需要本地 PostgreSQL"),
]


@pytest.mark.asyncio
async def test_five_v2_samples_route_and_online_matchers(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location(
        "online_sample_contracts", root / "backend/agent-service/tests/unit/test_diagnosis_sample_contracts.py"
    )
    contracts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contracts)
    sql = (root / "database/seeds/04_kbd_diagnosis_samples.sql").read_text()
    query = sql[sql.index("WITH sample_rows") : sql.index("INSERT INTO kbd_entry")]
    query += "SELECT target_support_id, target_signals_json FROM seed_rows WHERE target_sample_suite = 'kbd-semantic-entry-signal-v2'"
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text(query))).mappings().all()
        documents = [row["target_signals_json"] for row in rows]
        assert len(documents) == 5
        monkeypatch.setattr(contracts, "_documents", lambda: documents)
        monkeypatch.setattr(contracts, "SAMPLE_IDS", {row["target_support_id"] for row in rows})
        contracts.test_five_samples_pass_publish_review_and_online_cdd_compilation()
        contracts.test_five_samples_reach_supported_state_with_online_agent_matchers()
        for row in rows:
            document = row["target_signals_json"]
            result = await resolve_candidates(
                entries=[{"id": row["target_support_id"], "signals_json": document}],
                context="；".join(document["semantic_entry_profile"]["canonical_symptoms"]),
                strong_status="not_applicable",
            )
            assert result["decision"] == "executable", result
            assert result["candidates"][0]["kbd_id"] == row["target_support_id"]
    finally:
        await engine.dispose()
