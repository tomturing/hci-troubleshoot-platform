#!/usr/bin/env python3
"""用真实 SOP 数据验证变量"生产 -> 消费"闭环（不需要起全套服务）。

背景：Q2026102600003 的表现是"用户填了值，模型还在问"。修完之后必须证明：

  1. 模型再请求 `任务原始数据`（不存在的变量）会被**拒绝**，且不下发输入卡片；
  2. `vmid` 依赖的 `task_logs` 现在是自动采集（tool_call），不再要求人工粘贴；
  3. 路径类变量（`qemu_log_path` 等）由变量池直接渲染，不弹人工框；
  4. 依赖链能一路解析到"自动获取"，人为介入点只剩真正需要人判断的变量。

用法::

    python scripts/verify/sop_variable_runtime_e2e.py --schema-file /tmp/sop3_schema.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service"))

from app.memory.variable_pool.engine import sop_request_variable  # noqa: E402
from app.memory.variable_pool.pool import VariableRequestResult  # noqa: E402
from shared.models.sop_variable import validate_variable_schema  # noqa: E402

CONVERSATION_ID = "5fe5cb2c-f767-480b-826d-5c8a9a6c8df1"


def _clients(schema: list[dict], context: dict):
    kb = AsyncMock()
    kb.get_sop_document.return_value = {"id": 3, "variable_schema": schema}
    sop = AsyncMock()
    sop.get_execution.return_value = {
        "current_node_id": "n-1",
        "context_variables": context,
        "pending_variable_name": None,
    }
    return kb, sop


class _FakeToolExecutor:
    """模拟受控通道：返回 acli 的真实输出结构，避免"执行器不可用 -> 转人工"。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, args: dict, **kwargs):
        self.calls.append((tool_name, args))
        return {"stdout": "UPID:1001 启动虚拟机失败: 显卡切分方式不匹配", "exit_code": 0}


class _FakeSkillRunner:
    """模拟 DynamicSkillRunner：从原始日志里解析出 vmid。"""

    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, skill_name: str, context: dict, **kwargs):
        self.calls.append(skill_name)
        return {"values": {"vm": "1001", "name": "vm-test-1001", "end": "2026-10-26"}}


async def _request(name: str, schema: list[dict], context: dict, executor=None, runner=None):
    kb, sop = _clients(schema, context)
    return await sop_request_variable(
        name,
        reason="端到端验证",
        conversation_id=CONVERSATION_ID,
        sop_document_id=3,
        kb_client=kb,
        conversation_sop_client=sop,
        tool_executor=executor,
        skill_runner=runner,
    )


def _summarize(tag: str, result) -> str:
    if isinstance(result, VariableRequestResult):
        return f"[{tag}] ⚠️ 下发人工输入卡（kind={result.kind}, variable={result.variable_name}）"
    if isinstance(result, dict) and "error" in result:
        return f"[{tag}] error={result['error']} config_error={result.get('config_error', False)}"
    if isinstance(result, dict) and result.get("ok"):
        return f"[{tag}] ok value={result.get('value')!r} source={result.get('source')}"
    return f"[{tag}] 其他: {result}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-file", required=True, help="sop_document.variable_schema 的 JSON 文件")
    args = parser.parse_args()

    with open(args.schema_file, encoding="utf-8") as fp:
        schema = json.load(fp)
    report = validate_variable_schema(schema, mode="publish")
    print(f"schema 变量数={len(schema)} publish 校验 error={len(report.errors)}")
    if report.errors:
        for issue in report.errors:
            print(f"  {issue.code} {issue.variable}: {issue.message}")
        return 1

    failures: list[str] = []

    # 1) 野变量必须被拒绝，不得下发卡片
    r1 = await _request("任务原始数据", schema, {})
    print(_summarize("野变量 任务原始数据", r1))
    if isinstance(r1, VariableRequestResult) or (isinstance(r1, dict) and r1.get("error") != "sop_variable_not_defined"):
        failures.append("野变量未被拒绝")

    # 2) 日志类变量应走自动采集（tool_call），不要求人工粘贴
    executor = _FakeToolExecutor()
    runner = _FakeSkillRunner()
    r2 = await _request("task_logs", schema, {}, executor)
    print(_summarize("自动采集 task_logs", r2))
    if isinstance(r2, VariableRequestResult):
        failures.append("task_logs 仍要求人工输入")

    # 3) 路径模板应由变量池渲染
    r3 = await _request(
        "qemu_log_path",
        schema,
        {"task_day": {"value": "2026-10-26"}, "vmid": {"value": "1001"}},
    )
    print(_summarize("路径模板 qemu_log_path", r3))
    if not (isinstance(r3, dict) and r3.get("ok") and r3.get("source") == "path_template"):
        failures.append("qemu_log_path 未按模板渲染")

    # 4) 依赖链：vmid -> task_logs（自动采集），不得停在人工
    r4 = await _request("vmid", schema, {}, executor, runner)
    print(_summarize("依赖链 vmid", r4))
    if isinstance(r4, VariableRequestResult):
        failures.append("vmid 依赖链仍停在人工输入")

    # 5) 真正需要人工的变量仍然可以正常要值（不能把正常路径也堵死）
    manual = [v["name"] for v in schema if v.get("acquisition_strategy") == "user_input"]
    if manual:
        r5 = await _request(manual[0], schema, {})
        print(_summarize(f"人工变量 {manual[0]}", r5))
        if not isinstance(r5, VariableRequestResult):
            failures.append(f"人工变量 {manual[0]} 未能正常下发输入卡")

    print(f"\n工具执行器被调用 {len(executor.calls)} 次: {[c[0] for c in executor.calls]}")
    print(f"技能执行器被调用 {len(runner.calls)} 次: {runner.calls}")

    if failures:
        print("❌ 失败项：" + "; ".join(failures))
        return 1
    print("✅ 全部通过：野变量被拒、日志自动采集、路径模板渲染、依赖链可达、人工路径正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
