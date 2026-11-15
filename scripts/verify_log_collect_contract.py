#!/usr/bin/env python3
"""日志收集契约门禁：锁死「出处只有一个」。

用法：``uv run --no-project python scripts/verify_log_collect_contract.py``

检查分两类：
- 硬失败：任何一份手写字典卷土重来、契约闭合被破坏、禁词进入文案。
- 报告项：打印剩余的中文展示串数量，作为后续收敛的可对账基线，不阻断 CI。

为什么需要它：历史上「同一个 608 字节包」出现过三种口径，客户侧红卡、
管理端归因、顶部弹框又各自维护错误码与建议表。codegen 能保证生成物不漂移，
但保证不了有人再新建一个手写映射表。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "log_collect_display.json"

# 一旦这些文件不再由契约生成，或前端重新自建映射表，唯一出处即告失守。
GENERATED = [
    ROOT / "backend/case-service/app/domain/log_collect_display_generated.py",
    ROOT / "backend/case-service/app/services/log_collect_attribution_rules_generated.py",
    ROOT / "backend/shared/resolution/catalogs/log_collect_error_catalog.json",
    ROOT / "terminal_bridge/log_collect_contract_generated.go",
    ROOT / "frontend/shared/src/logCollectDisplay.ts",
    ROOT / "frontend/shared/src/logCollectUi.ts",
    ROOT / "frontend/customer/src/services/logCollectErrorHints.ts",
]
GENERATED_MARK = re.compile(r"自动生成|DO NOT EDIT|Code generated|_generated_from")

# 前端不得再出现的「手写映射表」声明；改成引用契约生成物是被允许的。
HANDWRITTEN_TABLES = re.compile(
    r"^const\s+(STATUS_TEXT|DELIVERY_TEXT|MODE_TEXT|bucketLabels)\s*:\s*Record<[\s\S]*?\n\}",
    re.M,
)
# 工单选择弹框也叫 STATUS_TEXT，但里面是 created/resolved 等工单状态，不属于日志收集域。
LOG_COLLECT_KEYS = re.compile(
    r"\b(collecting|packaging|delivered|acli_direct|bridge_relay|unfinished|partial)\b"
)


def _is_log_collect_file(path: Path) -> bool:
    """中文串基线只统计日志收集功能文件。

    只看文件名：worktree 目录本身叫 log-collect-single-source，若按父路径匹配
    会把整个前端都算进来，基线数字将失去对账意义。
    """
    name = path.name.lower()
    return "logcollect" in name or "logupload" in name
FRONTEND_GLOBS = [
    ROOT / "frontend/customer/src",
    ROOT / "frontend/admin/src",
]


def check_contract() -> tuple[list[str], dict]:
    problems: list[str] = []
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if contract.get("schema_version") != 3:
        problems.append("契约 schema_version 必须为 3")

    codes = contract["fault_catalog"]
    runtime = [c for c, v in codes.items() if v.get("kind") == "runtime"]
    signal = [c for c, v in codes.items() if v.get("kind") == "signal"]
    if not runtime or not signal:
        problems.append("码表必须同时含 runtime 与 signal 两类")

    for alias in contract.get("aliases", []):
        if alias["to"] not in codes:
            problems.append(f"别名目标不存在：{alias['to']}")
    for rule in contract.get("attribution_rules", []):
        if rule["code"] not in codes:
            problems.append(f"补码规则引用不存在的码：{rule['code']}")
    if not contract.get("attribution_rules"):
        problems.append("有序补码规则为空")

    policy = contract["ui"]["render_policy"]
    if policy.get("error_code_authoritative") is not True:
        problems.append("必须以 error_code 为归因权威，禁止用推断码覆盖")
    if policy.get("error_message_max_per_view") != 1:
        problems.append("同一视图 error_message 只允许出现 1 次")

    forbidden = contract["observability"]["forbidden_words"]
    blob = json.dumps({"ui": contract["ui"], "codes": codes}, ensure_ascii=False)
    for word in forbidden:
        if word in blob:
            problems.append(f"契约文案含命令式删除：{word}")

    # 唯一调用链：契约声明的表必须真的带 trace_id 列，否则按链路检索会断。
    schema = ROOT / "database" / "desired_schema.sql"
    if schema.exists():
        text = schema.read_text(encoding="utf-8").lower()
        for table in contract["observability"]["trace"]["required_in_tables"]:
            if table.lower() not in text:
                problems.append(f"声明式 schema 缺少链路表：{table}")
    return problems, contract


def check_generated() -> list[str]:
    problems: list[str] = []
    for path in GENERATED:
        if not path.exists():
            problems.append(f"生成物缺失：{path.relative_to(ROOT)}")
            continue
        head = path.read_text(encoding="utf-8")[:400]
        if not GENERATED_MARK.search(head):
            problems.append(f"生成物被人工改写（缺自动生成标记）：{path.relative_to(ROOT)}")
    return problems


def check_frontend() -> list[str]:
    problems: list[str] = []
    for base in FRONTEND_GLOBS:
        for pattern in ("*.ts", "*.vue"):
            for path in base.rglob(pattern):
                for match in HANDWRITTEN_TABLES.finditer(path.read_text(encoding="utf-8")):
                    if LOG_COLLECT_KEYS.search(match.group(0)):
                        problems.append(f"前端重新自建日志收集映射表：{path.relative_to(ROOT)}")
    return problems


def check_attribution() -> list[str]:
    path = ROOT / "backend/case-service/app/services/log_collect_error_attribution.py"
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    if "ATTRIBUTION_RULES" not in text:
        problems.append("补码规则未引用契约生成物")
    if '"场景清单目录缺失", "LC-SPC-001"' in text:
        problems.append("补码规则又回到手写状态")
    return problems


def report_legacy_chinese() -> int:
    """打印仍写在模板里的中文串数量：这是后续收敛的可对账基线，不是失败项。"""
    pattern = re.compile(r"[\u4e00-\u9fff]{4,}")
    total = 0
    for base in FRONTEND_GLOBS:
        for path in list(base.rglob("*.vue")) + list(base.rglob("*.ts")):
            if "__tests__" in str(path) or path.name.endswith(".spec.ts"):
                continue
            if not _is_log_collect_file(path):
                continue
            hits = pattern.findall(path.read_text(encoding="utf-8"))
            if hits:
                total += len(hits)
    return total


def main() -> int:
    problems, contract = check_contract()
    problems += check_generated()
    problems += check_frontend()
    problems += check_attribution()

    runtime = sum(1 for v in contract["fault_catalog"].values() if v.get("kind") == "runtime")
    signal = sum(1 for v in contract["fault_catalog"].values() if v.get("kind") == "signal")
    print(
        f"契约 v3：codes={len(contract['fault_catalog'])}（runtime={runtime} / signal={signal}），"
        f"matchers={len(contract['fault_matchers'])}，有序补码={len(contract['attribution_rules'])}"
    )
    print(f"仍写在前端模板中的中文串（后续收敛基线）：{report_legacy_chinese()}")
    if problems:
        for item in problems:
            print("FAIL:", item)
        return 1
    print("日志收集唯一出处门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
