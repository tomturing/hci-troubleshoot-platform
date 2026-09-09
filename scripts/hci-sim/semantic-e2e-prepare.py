#!/usr/bin/env python3
"""显式创建本地诊断样例验收副本；发布必须调用正式 HTTP 审核接口。"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
from dotenv import dotenv_values
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]
NON_FIXTURE_PRODUCER_TOOLS = {"qkv_case_context", "qkv_vm_console", "qkv_effect"}

SUITES = {
    "semantic": {
        "source_sample_suite": "kbd-semantic-entry-signal-v2",
        "runtime_suite_prefix": "semantic-e2e",
        "suite_label": "语义入口兜底 Signal v2 闭环验收",
        "title_prefix": "独立语义验收",
        "category_name": "语义验收",
        "category_source": "semantic_e2e",
        "entry_mode": "semantic_fallback",
    },
    "full": {
        "source_sample_suite": "diagnosis-signal-matrix-v1",
        "runtime_suite_prefix": "signal-v2-e2e",
        "suite_label": "在线/离线诊断 Signal v2 全量样例闭环验收",
        "title_prefix": "独立全量信号验收",
        "category_name": "全量信号验收",
        "category_source": "signal_v2_e2e",
        "entry_mode": "strong_signal",
    },
}


def diagnostic_signals(document: dict) -> list[dict]:
    """返回本轮诊断会实际调度的信号；修复后验证信号留待 remediation 阶段。"""

    return [
        signal
        for signal in document.get("signals", [])
        if (signal.get("orchestrate") or {}).get("phase", "diagnostic") == "diagnostic"
    ]


def executable_diagnostic_signals(document: dict) -> list[dict]:
    """返回会产生运行结果的诊断信号；case_context 只参与候选入口。"""

    return [
        signal
        for signal in diagnostic_signals(document)
        if (signal.get("acquire") or {}).get("tool") != "qkv_case_context"
    ]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--suite",
        choices=tuple(SUITES),
        default="semantic",
        help="semantic 验证语义入口；full 回归任务/告警/弹框等原始全量信号",
    )
    parser.add_argument("--publish", action="store_true", help="显式授权发布本次独立副本，不发布原样例")
    args = parser.parse_args()
    suite_config = SUITES[args.suite]
    # kb_category.code 只有 32 字符；E2E-<run-id>-<index> 需预留前后缀。
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,24}", args.run_id):
        raise SystemExit("run-id 只允许字母、数字、连字符，最多 24 字符")
    run_dir = ROOT / ".hci-sim-run" / "semantic-e2e" / args.run_id
    state_path = run_dir / "state.json"
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=config.get("POSTGRES_USER", "hci_admin"),
            password=config.get("POSTGRES_PASSWORD", "dev_password_123"),
            host="127.0.0.1",
            port=15432,
            database=config.get("POSTGRES_DB", "hci_troubleshoot"),
        )
    )

    def save(state):
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    try:
        if state_path.exists():
            state = json.loads(state_path.read_text())
            existing_kind = state.get("suite_kind")
            if existing_kind is None:
                sources = {str(item.get("source_support_id", "")) for item in state.get("cases", [])}
                existing_kind = "semantic" if sources and all(item.startswith("SAMPLE-V2-") for item in sources) else "full"
            if existing_kind != args.suite:
                raise SystemExit(
                    f"批次 {args.run_id} 已属于 {existing_kind} 样例，不能改作 {args.suite}；请使用新的 RUN_ID"
                )
            state.setdefault("suite_kind", existing_kind)
            state.setdefault("source_sample_suite", SUITES[existing_kind]["source_sample_suite"])
            state.setdefault("suite_label", SUITES[existing_kind]["suite_label"])
            state.setdefault("expected_case_count", 5)
        else:
            run_dir.mkdir(parents=True, mode=0o700)
            seed = (ROOT / "database/seeds/04_kbd_diagnosis_samples.sql").read_text()
            query = seed[seed.index("WITH sample_rows") : seed.index("INSERT INTO kbd_entry")]
            query += (
                "SELECT * FROM seed_rows WHERE target_sample_suite = "
                f"'{suite_config['source_sample_suite']}' ORDER BY target_support_id"
            )
            profile = json.loads((ROOT / "hci_sim/testdata/sample-suites/diagnosis-signal-matrix-v1.json").read_text())
            template_cases = profile["cases"]
            profile["cases"] = {}
            profile["sample_suite"] = f"{suite_config['runtime_suite_prefix']}-{args.run_id}"
            state = {
                "run_id": args.run_id,
                "suite_kind": args.suite,
                "source_sample_suite": suite_config["source_sample_suite"],
                "sample_suite": profile["sample_suite"],
                "suite_label": suite_config["suite_label"],
                "expected_case_count": 5,
                "cases": [],
                "results": {},
            }
            async with engine.begin() as connection:
                state["original_hashes"] = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT support_id, md5(to_jsonb(k)::text) FROM kbd_entry k WHERE support_id LIKE 'SAMPLE-%'"
                            )
                        )
                    ).all()
                )
                rows = (await connection.execute(text(query))).mappings().all()
                assert len(rows) == 5
                recovered_rows = (
                    await connection.execute(
                        text(
                            """
                            SELECT id, support_id, category_id, metadata
                            FROM kbd_entry
                            WHERE metadata ->> 'sample_suite' = :sample_suite
                              AND metadata ->> 'run_id' = :run_id
                            ORDER BY support_id
                            """
                        ),
                        {"sample_suite": state["sample_suite"], "run_id": args.run_id},
                    )
                ).mappings().all()
                if recovered_rows and len(recovered_rows) != 5:
                    raise SystemExit(
                        f"检测到不完整的同批次数据（{len(recovered_rows)}/5），拒绝自动覆盖，请人工核对"
                    )
                recovered_by_source = {
                    str(item["metadata"].get("source_support_id")): item for item in recovered_rows
                }
                for index, row in enumerate(rows):
                    # 确定性 ID 让“数据库已提交、state.json 尚未落盘”的进程中断可恢复；
                    # 不再用秒级时间戳制造碰撞或重复副本。
                    identity = hashlib.sha256(f"{args.run_id}:{row['target_support_id']}".encode()).hexdigest()
                    support_id = f"99{int(identity[:13], 16) % 10**16:016d}"
                    category_id = f"E2E-{args.run_id}-{index}"
                    document = copy.deepcopy(row["target_signals_json"])
                    document["verification_contract"]["case_id"] = support_id
                    source_id = row["target_support_id"]
                    profile_case_id = (profile.get("support_aliases") or {}).get(source_id, source_id)
                    card = copy.deepcopy(template_cases[profile_case_id])
                    product_version = str(card.get("product_version") or "").strip()
                    if not product_version:
                        raise SystemExit(f"场景画像 {profile_case_id} 缺少 product_version")
                    # 独立副本的 Scope 与同一次仿真的产品版本必须一致。不能保留
                    # `sample` 或旧版本占位，否则发布可通过、Runtime 却会在命令前拒绝。
                    document["verification_contract"].setdefault("scope", {})["versions"] = [product_version]
                    recovered = recovered_by_source.get(source_id)
                    if recovered:
                        support_id = str(recovered["support_id"])
                        category_id = str(recovered["category_id"])
                        document["verification_contract"]["case_id"] = support_id
                    values = {
                        field: row[field]
                        for field in (
                            "problem_description",
                            "alert_info",
                            "steps_text",
                            "root_cause",
                            "solution",
                        )
                    }
                    values.update(
                        support_id=support_id,
                        title=f"【{suite_config['title_prefix']} {args.run_id}】{row['title']}",
                        signals_json=json.dumps(document, ensure_ascii=False),
                        category_id=category_id,
                        content_md="\n\n".join(
                            str(row[field] or "")
                            for field in ("problem_description", "alert_info", "steps_text", "root_cause", "solution")
                        ),
                        metadata=json.dumps(
                            {
                                "is_test_sample": True,
                                "test_only": True,
                                "sample_suite": state["sample_suite"],
                                "source_support_id": source_id,
                                "source_sample_suite": suite_config["source_sample_suite"],
                                "sample_entry_mode": suite_config["entry_mode"],
                                "run_id": args.run_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    if recovered:
                        kbd_id = int(recovered["id"])
                    else:
                        await connection.execute(
                            text("""
                            INSERT INTO kb_category (code, name, domain, path_labels, level, source, version, is_active)
                            VALUES (:code, :name, '测试', CAST(:labels AS jsonb), 1, :source, '1.0', true)
                        """),
                            {
                                "code": category_id,
                                # 隔离分类必须表达整篇案例，而不是只写一个宽泛领域。
                                # 例如“网络 + 存储”联合案例若仅命名为“存储”，S0 会稳定
                                # 推荐另一个“网络”测试分类，导致完整在线链路无法选择目标 KBD。
                                "name": f"{suite_config['category_name']} {args.run_id} {row['title']}",
                                "labels": json.dumps([suite_config["suite_label"], row["domain_hint"]], ensure_ascii=False),
                                "source": suite_config["category_source"],
                            },
                        )
                        kbd_id = (
                            await connection.execute(
                                text("""
                            INSERT INTO kbd_entry (support_id, title, problem_description, alert_info, steps_text,
                                root_cause, solution, signals_json, category_id, ai_category_id, content_md, content_raw, metadata, status)
                            VALUES (:support_id, :title, :problem_description, :alert_info, :steps_text,
                                :root_cause, :solution, CAST(:signals_json AS jsonb), :category_id, :category_id,
                                :content_md, :content_md, CAST(:metadata AS jsonb), 'draft') RETURNING id
                        """),
                                values,
                            )
                        ).scalar_one()
                    # hci-sim 的静态 Fixture 只描述可冻结 argv 的直接信号。
                    # case_context 没有执行器；vm_console/effect 编译为专用 Intent，
                    # 分别由固定截图通道和效果声明通道验收，不能伪造成普通命令。
                    execution_signals = executable_diagnostic_signals(document)
                    signal_ids = {
                        s["id"]
                        for s in execution_signals
                        if s["acquire"]["tool"] not in NON_FIXTURE_PRODUCER_TOOLS
                    }
                    card["signals"] = {key: value for key, value in card["signals"].items() if key in signal_ids}
                    assert set(card["signals"]) == signal_ids
                    if args.suite == "semantic":
                        card["fault_description"] = "；".join(
                            document["semantic_entry_profile"]["canonical_symptoms"]
                        )
                    profile["cases"][support_id] = card
                    signal_tools = {
                        str(signal["id"]): str(signal["acquire"]["tool"])
                        for signal in execution_signals
                    }
                    state["cases"].append(
                        {
                            "kbd_id": kbd_id,
                            "support_id": support_id,
                            "source_support_id": source_id,
                            "category_id": category_id,
                            "description": card["fault_description"],
                            "entry_mode": suite_config["entry_mode"],
                            "source_sample_suite": suite_config["source_sample_suite"],
                            "signal_tools": signal_tools,
                            "expected_signal_count": len(signal_tools),
                        }
                    )
            (run_dir / "scenario-profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
            save(state)
        # 兼容已经创建的隔离批次：仅允许修正本 run-id、test_only KBD 所属的
        # 独立验收分类名称。它不会改原样例、正式分类或案例信号。
        active_config = SUITES[state["suite_kind"]]
        async with engine.begin() as connection:
            for case in state["cases"]:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT title
                            FROM kbd_entry
                            WHERE id = :kbd_id
                              AND metadata ->> 'run_id' = :run_id
                              AND metadata ->> 'test_only' = 'true'
                            """
                        ),
                        {"kbd_id": case["kbd_id"], "run_id": args.run_id},
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise SystemExit("隔离批次 KBD 身份校验失败，拒绝更新分类")
                base_title = re.sub(r"^【独立(?:语义|全量信号)验收 [^】]+】", "", str(row["title"]))
                case["title"] = base_title
                updated = (
                    await connection.execute(
                        text(
                            """
                            UPDATE kb_category
                            SET name = :name
                            WHERE code = :category_id AND source = :source
                            RETURNING code
                            """
                        ),
                        {
                            "name": f"{active_config['category_name']} {args.run_id} {base_title}",
                            "category_id": case["category_id"],
                            "source": active_config["category_source"],
                        },
                    )
                ).scalar_one_or_none()
                if updated is None:
                    raise SystemExit("隔离分类身份校验失败，拒绝更新")
        save(state)
        token = config.get("INTERNAL_API_TOKEN", "hci-dev-internal-token")
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18004", timeout=180, headers={"Authorization": f"Bearer {token}"}
        ) as client:
            for case in state["cases"]:
                if args.publish:
                    detail = await client.get(f"/api/admin/kbd/{case['kbd_id']}")
                    detail.raise_for_status()
                    if detail.json()["status"] == "draft":
                        response = await client.post(
                            f"/api/admin/kbd/{case['kbd_id']}/approve",
                            json={
                                "reviewer_id": 95394,
                                "review_note": f"用户授权的{state['suite_label']}副本 {args.run_id}",
                                "category_id": case["category_id"],
                                "lock_version": detail.json().get("lock_version", 1),
                            },
                        )
                        state["results"][case["support_id"]] = {
                            "publish_http_status": response.status_code,
                            "publish": response.json(),
                        }
                        save(state)
                        print(
                            json.dumps(
                                {"support_id": case["support_id"], "publish_http_status": response.status_code},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        response.raise_for_status()
                response = await client.get(f"/api/kb/hci-sim/capabilities/{case['support_id']}")
                response.raise_for_status()
                capability = response.json()
                state["results"].setdefault(case["support_id"], {})["capability"] = capability
                save(state)
                print(
                    json.dumps(
                        {
                            "support_id": case["support_id"],
                            "source_support_id": case["source_support_id"],
                            "suite_kind": state["suite_kind"],
                            "capability": capability["status"],
                            "gaps": capability.get("capability_gaps"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        # 每次刷新 Capability 后都从权威 synthetic_routes 重建运行时画像，使旧批次
        # 也能随“普通命令 / 专用 Intent”边界演进而恢复，不要求删除审计数据重建。
        base_profile = json.loads(
            (ROOT / "hci_sim/testdata/sample-suites/diagnosis-signal-matrix-v1.json").read_text()
        )
        template_cases = base_profile["cases"]
        base_profile["cases"] = {}
        base_profile["sample_suite"] = state["sample_suite"]
        for case in state["cases"]:
            source_id = case["source_support_id"]
            profile_case_id = (base_profile.get("support_aliases") or {}).get(source_id, source_id)
            card = copy.deepcopy(template_cases[profile_case_id])
            routes = (
                state["results"]
                .get(case["support_id"], {})
                .get("capability", {})
                .get("resolved", {})
                .get("synthetic_routes", [])
            )
            route_ids = {str(item.get("signal_id")) for item in routes if item.get("signal_id")}
            card["signals"] = {key: value for key, value in card["signals"].items() if key in route_ids}
            if set(card["signals"]) != route_ids:
                raise SystemExit(f"场景画像无法覆盖 {source_id} 的冻结 synthetic_routes")
            if state["suite_kind"] == "semantic":
                card["fault_description"] = case["description"]
            base_profile["cases"][case["support_id"]] = card
        profile_path = run_dir / "scenario-profile.json"
        profile_path.write_text(json.dumps(base_profile, ensure_ascii=False, indent=2) + "\n")
        os.chmod(profile_path, 0o600)
        async with engine.connect() as connection:
            current = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT support_id, md5(to_jsonb(k)::text) FROM kbd_entry k WHERE support_id LIKE 'SAMPLE-%'"
                        )
                    )
                ).all()
            )
        assert current == state["original_hashes"], "原样例发生变化，需要核对并发操作"
        print(f"original_samples_unchanged=PASS state={state_path}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
