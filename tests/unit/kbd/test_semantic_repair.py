"""KBD 结构语义扫描与三方修复测试。"""

from __future__ import annotations

import json
from pathlib import Path

from kbd.converter import _build_image_seq_map
from kbd.semantic_repair import (
    _apply_explicit_resolutions,
    _merge_image_contexts,
    _merge_text,
    _plan_entry,
    _section_issues,
    _sha256_text,
)


def test_section_issues_detects_nested_code_order_and_image_shift():
    image_url = "https://support.sangfor.com.cn/_static/a.png"
    html = f"""
    <ol><li><p>检查日志</p><ul><li>判断依据：<pre>关键错误</pre></li></ul>
    <p><img src="{image_url}" /></p></li><li>下一步</li></ol>
    """
    current = "- 检查日志 ![img:0]\n  - 判断依据：\n- 下一步"
    generated = "1. 检查日志\n   - 判断依据：\n     ```text\n关键错误\n```\n   ![img:0]\n2. 下一步"

    issues = _section_issues(html, current, generated, _build_image_seq_map(html))

    assert {issue["kind"] for issue in issues} == {
        "missing_code",
        "ordered_list_lost",
        "image_anchor_shifted",
    }


def test_section_issues_detects_code_content_without_fence_boundary():
    html = "<div><pre>第一行\n第二行</pre></div>"

    issues = _section_issues(html, "第一行\n第二行", "```text\n第一行\n第二行\n```", {})

    assert [issue["kind"] for issue in issues] == ["code_block_boundary_lost"]


def test_merge_text_replaces_untouched_baseline():
    merged, status = _merge_text("旧转换结果", "旧转换结果", "修复后结果")

    assert merged == "修复后结果"
    assert status == "source_replaced"


def test_merge_text_preserves_non_overlapping_expert_change():
    baseline = "第一行\n第二行\n第三行"
    current = "第一行（专家修订）\n第二行\n第三行"
    generated = "第一行\n第二行\n第三行\n新增代码块"

    merged, status = _merge_text(current, baseline, generated)

    assert status == "merged"
    assert merged == "第一行（专家修订）\n第二行\n第三行\n新增代码块"


def test_merge_text_reports_overlapping_conflict():
    merged, status = _merge_text("专家版本", "共同基线", "源站修复版本")

    assert merged is None
    assert status == "conflict"


def test_explicit_resolution_requires_current_hash_and_applies_reviewed_replacement():
    current = {"steps_text": "专家当前稿"}
    generated = {"steps_text": "源站管理面措辞"}
    resolution = {
        "expected_current_hashes": {"steps_text": _sha256_text(current["steps_text"])},
        "fields": {
            "steps_text": {
                "strategy": "generated",
                "replacements": [{"from": "管理面", "to": "后台"}],
            }
        },
    }

    resolved, unresolved = _apply_explicit_resolutions(
        support_id="32866",
        conflicts=["steps_text"],
        current=current,
        generated=generated,
        resolution=resolution,
    )

    assert unresolved == []
    assert resolved == {"steps_text": "源站后台措辞"}


def test_merge_image_contexts_preserves_evidence_and_updates_position_context():
    current = [
        {
            "seq": 2,
            "section": "steps_text",
            "context_before": "错误旧上下文",
            "context_after": "旧上下文",
            "desc": "识图结果",
            "evidence": {"quality": {"status": "success"}},
        }
    ]
    generated = [
        {
            "seq": 2,
            "section": "steps_text",
            "context_before": "代码块之后",
            "context_after": "下一步骤",
            "desc": "",
        }
    ]

    merged = _merge_image_contexts(current, generated, {"steps_text"})

    assert merged[0]["context_before"] == "代码块之后"
    assert merged[0]["context_after"] == "下一步骤"
    assert merged[0]["desc"] == "识图结果"
    assert merged[0]["evidence"] == {"quality": {"status": "success"}}


def test_kbd32866_plan_is_ready_when_current_equals_legacy_baseline(tmp_path):
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "kbd" / "32866_semantic.html"
    case_dir = tmp_path / "32866"
    case_dir.mkdir(parents=True)
    (case_dir / "raw.json").write_text(
        json.dumps({"id": 32866, "name": "KBD32866 回归", "content": fixture.read_text(encoding="utf-8")}),
        encoding="utf-8",
    )
    baseline = {
        "problem_description": "镜像分发失败。",
        "alert_info": "告警一\n告警二",
        "steps_text": "- 检查 nerdctl 执行失败。 ![img:2]\n- 检查远端拷贝失败。 ![img:3]",
        "root_cause": "运行环境缺少 nerdctl 路径。",
        "solution": "- 进入容器。\n- 修改函数。 ![img:4]\n- 保存文件。",
        "operational_impact": "影响镜像分发。",
        "is_temporary": "是。",
        "recommendations": "升级正式补丁。",
        "images_json": [
            {"seq": seq, "section": "steps_text" if seq in {2, 3} else "solution", "desc": ""} for seq in range(5)
        ],
    }
    row = {
        "id": 1047,
        "support_id": "32866",
        "status": "draft",
        "lock_version": 2,
        "title": "KBD32866 回归",
        "working_payload": None,
        "baseline_payload": baseline,
        **baseline,
    }

    plan = _plan_entry(row, tmp_path)

    assert plan.report["plan_status"] == "ready"
    assert set(plan.report["issues"]) == {"alert_info", "steps_text", "solution"}
    assert plan.updates["steps_text"].index("exec of nerdctl failed") < plan.updates["steps_text"].index("![img:2]")
    assert plan.updates["solution"].index("sub check_remote_host_is_docker") < plan.updates["solution"].index(
        "![img:4]"
    )
    assert plan.updates["reviewed_image_seqs"] == []
    assert plan.updates["lock_version"] == 2
