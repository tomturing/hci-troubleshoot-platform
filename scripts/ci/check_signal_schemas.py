#!/usr/bin/env python3
"""§6.1 CI 契约校验：schema 自身合法 + fixtures 校验 + 漂移检测。

校验内容：
  1. 加载 backend/shared/schemas/signals/ 下所有 *.schema.json，确认可被 referencing 解析（自身合法）。
  2. 用 signal.v2.schema.json 校验内置 fixtures：
     - 合法样本必须通过（证明契约能接收真实信号）；
     - 非法样本（顶层 keyword / 缺必填 / 幽灵字段）必须被拒（证明 §6 不变量被强制）。
  3. 漂移检测：调用 gen-schemas.build_all() 重新生成内存表示，与入库文件逐一对
     比；不一致则失败并提示 `make gen-schemas`（防止 ACQUIRER_ARGS_SCHEMA 变更后
     未重新导出契约文件）。

退出码：0=通过，1=失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # scripts/ci -> scripts -> repo root
GEN_SCRIPT_DIR = REPO_ROOT / "backend" / "scripts"
SCHEMA_DIR = REPO_ROOT / "backend" / "shared" / "schemas" / "signals"

sys.path.insert(0, str(GEN_SCRIPT_DIR))


def _load_registry():
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7

    resources = []
    for p in sorted(SCHEMA_DIR.rglob("*.schema.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        uri = data.get("$id")
        if not uri:
            uri = p.relative_to(SCHEMA_DIR).as_posix()
        resources.append((uri, Resource.from_contents(data, default_specification=DRAFT7)))
    return Registry().with_resources(resources), resources


def _main() -> int:
    import importlib.util
    import jsonschema

    # gen-schemas.py 文件名含连字符，无法直接 import，用 importlib 动态加载
    spec = importlib.util.spec_from_file_location(
        "gen_schemas", GEN_SCRIPT_DIR / "gen-schemas.py"
    )
    gen_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_mod)
    build_all = gen_mod.build_all

    if not SCHEMA_DIR.exists():
        print(f"[schema] 契约目录不存在: {SCHEMA_DIR}")
        return 1

    registry, loaded = _load_registry()
    print(f"[schema] 已加载 {len(loaded)} 个契约文件（自身合法）")

    schema_v2 = json.loads((SCHEMA_DIR / "signal.v2.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema_v2, registry=registry)

    # 合法样本：1 条 frontend（qkv）+ 1 条 backend（qfk，含 match/orchestrate/provenance/review）
    valid = {
        "schema_version": 2,
        "signals": [
            {
                "acquire": {
                    "tool": "qkv_task",
                    "args": {"keyword": "启动虚拟机", "limit": 100, "is_failed": True, "timeout": 10},
                }
            },
            {
                "acquire": {
                    "tool": "qfk_log",
                    "args": {
                        "resource_keyword": "vgpu",
                        "host": "asv",
                        "file": "/var/log/x.log",
                        "time_window": "-1h",
                        "timeout": 10,
                    },
                },
                "match": {"type": "keyword", "pattern": "绑定vgpu命令失败", "mode": "any", "expected": True},
                "orchestrate": {"produces": [{"name": "X", "path": "$.x"}], "requires": ["Y"], "phase": "diag"},
                "provenance": {"category": "backend", "confidence": 0.9, "risk": 2},
                "review": {"require_human_confirm": True},
            },
        ],
    }
    try:
        validator.validate(valid)
    except jsonschema.ValidationError as exc:
        print(f"[fixture] 合法样本被错误拒绝: {exc.message}")
        return 1
    print("[fixture] 合法样本通过 ✓")

    # 非法样本：每条都应被 jsonschema 拒绝
    invalid_cases = {
        "顶层 keyword 幽灵字段": {
            "schema_version": 2,
            "signals": [{"keyword": "x", "acquire": {"tool": "qkv_task", "args": {"keyword": "y"}}}],
        },
        "acquire.args 缺必填 keyword": {
            "schema_version": 2,
            "signals": [{"acquire": {"tool": "qkv_task", "args": {"limit": 10}}}],
        },
        "acquire.args 幽灵字段 bogus": {
            "schema_version": 2,
            "signals": [{"acquire": {"tool": "qkv_task", "args": {"keyword": "k", "bogus": 1}}}],
        },
    }
    for name, doc in invalid_cases.items():
        try:
            validator.validate(doc)
            print(f"[fixture] 非法样本 '{name}' 应被拒绝却通过 ✗")
            return 1
        except jsonschema.ValidationError:
            pass
    print(f"[fixture] {len(invalid_cases)} 个非法样本均被正确拒绝 ✓")

    # 漂移检测：重新导出并与入库文件对比
    memory = build_all()
    drift = False
    for rel, doc in memory.items():
        p = SCHEMA_DIR / rel
        if not p.exists():
            print(f"[drift] 缺失文件: {rel}（请运行 make gen-schemas）")
            drift = True
            continue
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        if on_disk != doc:
            print(f"[drift] {rel} 与代码导出不一致，请运行 make gen-schemas")
            drift = True
    if drift:
        return 1
    print("[drift] 契约文件与代码导出一致 ✓")
    print("OK: 信号 v2 JSON Schema 契约校验全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
