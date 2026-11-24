"""回归：清单快照的写入不得被「质量结论」的门槛挡住。

现场（2026-11-24 staging，5 条任务）：
``put_artifact_manifest`` 把清单快照与质量结论塞进同一条 jsonb 更新，再用

    WHERE detail ->> 'artifact_quality' IS DISTINCT FROM 'ok' OR :quality = 'ok'

整体挡住。质量一旦被判过 ok，后续上报的清单快照就连带丢失——而清单事实
其实已经写进 ``log_collect_artifact_manifest``。客户面板只拿任务行，
读不到 ``detail.artifact_manifest`` 就把 S7 判成「未知」，即使证据早已到达。

契约：快照无条件写；质量保留「已 ok 不被更差结论覆盖」的语义。
"""

from __future__ import annotations

import inspect

from app.routes.log_collect_evidence import put_artifact_manifest


def _update_sql() -> str:
    """取出该函数里那条任务行更新的 SQL（含 WHERE），并剥掉 SQL 注释。

    必须剥注释：本次修复的注释里就写了历史条件原文，若把注释算进来，
    「WHERE 不得含 artifact_quality」这类断言会被注释正文误判。
    """
    source = inspect.getsource(put_artifact_manifest)
    start = source.index("UPDATE log_collect_task")
    end = source.index('"""', start)
    sql = source[start:end]
    kept = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return "\n".join(kept)


def test_manifest_snapshot_is_written_without_quality_gate() -> None:
    sql = _update_sql()
    body, sep, where = sql.partition("WHERE task_id = :task_id")
    assert sep, "SQL 结构变了，本用例需要跟着改"

    # 1) 快照必须在 SET 主体里，且不依赖任何条件
    assert "'artifact_manifest', CAST(:manifest_snapshot AS jsonb)" in body
    # 2) WHERE 里不允许再出现质量判断——这正是当年吞掉快照的那道门
    assert "artifact_quality" not in where, "清单快照不得被质量门槛挡住"


def test_quality_still_protected_by_case_branch() -> None:
    body = _update_sql().partition("WHERE task_id = :task_id")[0]
    case_start = body.index("CASE")
    case_end = body.index("END")
    snapshot_at = body.index("'artifact_manifest'")
    quality_at = body.index("'artifact_quality'")

    # 快照写在 CASE 之前 = 无条件；质量写在 CASE 内 = 仍受保护
    assert snapshot_at < case_start < quality_at < case_end
