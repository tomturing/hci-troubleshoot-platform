#!/usr/bin/env python3
"""从 contracts/log_collect_display.json 生成全平台日志收集常量与文案。

单一来源：契约 JSON（schema v3）。产物共七份，覆盖 Go / Python / TS / SQL 消费端：
    backend/case-service/app/domain/log_collect_display_generated.py
    backend/case-service/app/services/log_collect_attribution_rules_generated.py
    backend/shared/resolution/catalogs/log_collect_error_catalog.json
    terminal_bridge/log_collect_contract_generated.go
    frontend/shared/src/logCollectDisplay.ts
    frontend/shared/src/logCollectUi.ts
    frontend/customer/src/services/logCollectErrorHints.ts

用法：
    python3 scripts/gen_log_collect_display.py           # 写入产物
    python3 scripts/gen_log_collect_display.py --check   # 仅校验，漂移则退出码 1

为什么不手写第二份：状态文案、体积格式化、阈值一旦两侧各写一份，必然漂移
（历史上同一个 608 字节包出现过 608 B / 0.0 MB / 608 字节三种口径）。
运行时 71 码、证据派生 20 码、客户检查/处理建议、无数据提示与呈现策略同样只认这一份。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "contracts" / "log_collect_display.json"
PY_OUT = (
    REPO_ROOT
    / "backend"
    / "case-service"
    / "app"
    / "domain"
    / "log_collect_display_generated.py"
)
TS_OUT = REPO_ROOT / "frontend" / "shared" / "src" / "logCollectDisplay.ts"
# 以下文件原本由人工维护，v3 起全部改为契约生成物：全仓库只允许一份手写字典。
CATALOG_OUT = (
    REPO_ROOT / "backend" / "shared" / "resolution" / "catalogs"
    / "log_collect_error_catalog.json"
)
HINTS_OUT = (
    REPO_ROOT / "frontend" / "customer" / "src" / "services"
    / "logCollectErrorHints.ts"
)
UI_OUT = REPO_ROOT / "frontend" / "shared" / "src" / "logCollectUi.ts"
RULES_OUT = (
    REPO_ROOT / "backend" / "case-service" / "app" / "services"
    / "log_collect_attribution_rules_generated.py"
)
GO_OUT = REPO_ROOT / "terminal_bridge" / "log_collect_contract_generated.go"

PY_HEADER = '''"""自动生成，请勿手改。

源：contracts/log_collect_display.json
生成：python3 scripts/gen_log_collect_display.py
"""

from __future__ import annotations

'''

PY_FOOTER = '''

def format_size(value: object) -> str:
    """统一的体积展示口径。

    None / 非数字 -> 缺失占位；0 -> "0 B"（0 是事实，不是缺失）；
    >= 1 KiB 按 1024 进制，<10 两位、<100 一位、其余整数位。
    """
    if value is None:
        return SIZE_MISSING_TEXT
    try:
        size = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return SIZE_MISSING_TEXT
    if size != size or size < 0:  # NaN
        return SIZE_MISSING_TEXT
    if size < SIZE_BASE:
        return f"{round(size):d} B"
    index = 0
    scaled = size
    while scaled >= SIZE_BASE and index < len(SIZE_UNITS) - 1:
        scaled /= SIZE_BASE
        index += 1
    if scaled < 10:
        digits = SIZE_DIGITS_LT_10
    elif scaled < 100:
        digits = SIZE_DIGITS_LT_100
    else:
        digits = SIZE_DIGITS_GTE_100
    return f"{scaled:.{digits}f} {SIZE_UNITS[index]}"


def format_fault_text(code: str) -> str:
    """客户侧错误文案：``组件标签：问题描述；建议动作``。

    一句话只说一件事：哪个组件、出了什么问题、客户下一步做什么。
    组件标签用短名（如 ``HCI环境``），管理员侧另用 ``component_tag`` 带段前缀。
    """
    item = FAULT_CATALOG.get(code) or FAULT_CATALOG[FAULT_DEFAULT_CODE]
    component = COMPONENTS[item["component"]]
    return f"{component['label']}：{item['problem']}；{item['action']}"


def _fault_text(message: object) -> str:
    """把任意错误载荷规整为可匹配文本。

    dict 只取错误相关字段，避免整份 repr 里的正常字段名（如 available_mb）
    被当成故障关键词；列表只取前 8 项，防止超长载荷拖慢匹配。
    """
    if isinstance(message, dict):
        parts = [message.get(key) for key in ("error", "message", "reason", "detail")]
        return " ".join(str(part) for part in parts if part)
    if isinstance(message, (list, tuple)):
        return " ".join(str(part) for part in message[:8])
    return str(message or "")


def classify_fault(message: object) -> str:
    """把原始错误文本归类到组件错误码；无命中时如实给「平台未给出可识别的错误码」。

    取「最长命中关键词」而非首个命中：更长的关键词是更具体的证据，
    避免 ``terminal_bridge`` / ``401`` 这类泛词抢走 x509、票据错误的归因。
    同长度时按契约顺序取先者。
    """
    text = _fault_text(message).lower()
    best_code = FAULT_DEFAULT_CODE
    best_len = 0
    for matcher in FAULT_MATCHERS:
        hit = max(
            (len(keyword) for keyword in matcher["keywords"] if keyword.lower() in text),
            default=0,
        )
        if hit > best_len:
            best_len, best_code = hit, matcher["code"]
    return best_code


def fault_detail(code: str, *, trace_id: str | None = None) -> dict:
    """首个异常点的完整归因：客户文案 + 管理员判据 + 唯一调用链。"""
    resolved = code if code in FAULT_CATALOG else FAULT_DEFAULT_CODE
    item = FAULT_CATALOG[resolved]
    component = COMPONENTS[item["component"]]
    stage = STAGES[item["stage"]]
    return {
        "code": resolved,
        "scope": component["scope"],
        "scope_label": SCOPE_LABELS.get(component["scope"], SCOPE_LABELS["cloud"]),
        "component": item["component"],
        "component_label": component["label"],
        "component_tag": component["tag"],
        "stage": item["stage"],
        "stage_label": stage["label"],
        "problem": item["problem"],
        "action": item["action"],
        "customer_text": format_fault_text(resolved),
        "admin_detail": item["admin_detail"],
        "evidence": item["evidence"],
        "trace_id": trace_id,
    }
'''

TS_HEADER = """/**
 * 自动生成，请勿手改。
 *
 * 源：contracts/log_collect_display.json
 * 生成：python3 scripts/gen_log_collect_display.py
 */

"""

TS_FOOTER = """
/** 统一的体积展示口径（与后端 format_size 逐字对齐）。 */
export function formatLogCollectSize(value: number | null | undefined): string {
  if (value === null || value === undefined) return SIZE_MISSING_TEXT
  const size = Number(value)
  if (!Number.isFinite(size) || size < 0) return SIZE_MISSING_TEXT
  if (size < SIZE_BASE) return `${Math.round(size)} B`
  let index = 0
  let scaled = size
  while (scaled >= SIZE_BASE && index < SIZE_UNITS.length - 1) {
    scaled /= SIZE_BASE
    index += 1
  }
  const digits = scaled < 10 ? SIZE_DIGITS_LT_10 : scaled < 100 ? SIZE_DIGITS_LT_100 : SIZE_DIGITS_GTE_100
  return `${scaled.toFixed(digits)} ${SIZE_UNITS[index]}`
}

/** 客户侧错误文案：组件标签：问题描述；建议动作。 */
export function formatFaultText(code: string): string {
  const item = FAULT_CATALOG[code as FaultCode] ?? FAULT_CATALOG[FAULT_DEFAULT_CODE]
  const component = COMPONENTS[item.component as ComponentKey]
  return `${component.label}：${item.problem}；${item.action}`
}

/** 把任意错误载荷规整为可匹配文本（与后端 _fault_text 行为对齐）。 */
function faultText(message: unknown): string {
  if (Array.isArray(message)) return message.slice(0, 8).map((part) => String(part)).join(' ')
  if (message && typeof message === 'object') {
    const record = message as Record<string, unknown>
    return ['error', 'message', 'reason', 'detail']
      .map((key) => record[key])
      .filter((part) => part !== undefined && part !== null && part !== '')
      .map((part) => String(part))
      .join(' ')
  }
  return String(message ?? '')
}

/** 把原始错误文本归类到组件错误码，取最长命中关键词（与后端 classify_fault 行为对齐）。 */
export function classifyFault(message: unknown): FaultCode {
  const text = faultText(message).toLowerCase()
  let bestCode = FAULT_DEFAULT_CODE as FaultCode
  let bestLength = 0
  for (const matcher of FAULT_MATCHERS) {
    let hit = 0
    for (const keyword of matcher.keywords) {
      if (text.includes(keyword.toLowerCase())) hit = Math.max(hit, keyword.length)
    }
    if (hit > bestLength) {
      bestLength = hit
      bestCode = matcher.code as FaultCode
    }
  }
  return bestCode
}

/** 首个异常点的完整归因；字段名与后端 API 一致，统一 snake_case。 */
export function faultDetail(code: string, traceId?: string | null) {
  const resolved: FaultCode = (code in FAULT_CATALOG ? code : FAULT_DEFAULT_CODE) as FaultCode
  const item = FAULT_CATALOG[resolved]
  const component = COMPONENTS[item.component as ComponentKey]
  const stage = STAGES[item.stage as StageKey]
  return {
    code: resolved,
    scope: component.scope,
    scope_label: SCOPE_LABELS[component.scope as ScopeKey] ?? SCOPE_LABELS.cloud,
    component: item.component,
    component_label: component.label,
    component_tag: component.tag,
    stage: item.stage,
    stage_label: stage.label,
    problem: item.problem,
    action: item.action,
    customer_text: formatFaultText(resolved),
    admin_detail: item.admin_detail,
    evidence: item.evidence,
    trace_id: traceId ?? null,
  }
}
"""


def _py_literal(name: str, value: object) -> str:
    # 必须用 Python 字面量而非 JSON：运行时码里含布尔 retryable / 空值，
    # json.dumps 会写出 false/null，在 Python 源码里是 NameError。
    import pprint

    return f"{name} = {pprint.pformat(value, indent=4, sort_dicts=False, width=100)}\n"


def _ts_literal(name: str, value: object) -> str:
    return f"export const {name} = {json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)} as const\n"


def _extra_python(contract: dict, thresholds: dict) -> list[str]:
    return [
        _py_literal("SCOPE_LABELS", contract["scope"]),
        "\n",
        _py_literal("COMPONENTS", contract["component"]),
        "\n",
        # 常量本身按 key 排序输出以保证 diff 稳定，业务展示顺序单独用契约声明顺序。
        f"COMPONENT_ORDER = {json.dumps(list(contract['component']), ensure_ascii=False)}\n",
        "\n",
        _py_literal("STAGES", contract["stage"]),
        "\n",
        _py_literal("STAGE_STATUS_LABELS", contract["stage_status"]),
        "\n",
        _py_literal("FAULT_CATALOG", contract["fault_catalog"]),
        "\n",
        _py_literal("FAULT_MATCHERS", contract["fault_matchers"]),
        "\n",
        _py_literal("FAULT_ATTRIBUTION", contract["fault_attribution"]),
        "\n",
        f"FAULT_DEFAULT_CODE = {contract['fault_attribution']['default_code']!r}\n",
        f"ARCHIVE_VERIFY_WAIT_MINUTES = {int(thresholds['archive_verify_wait_minutes'])}\n",
        "\n",
    ]


def _extra_ts(contract: dict, thresholds: dict) -> list[str]:
    return [
        _ts_literal("SCOPE_LABELS", contract["scope"]),
        "\n",
        _ts_literal("COMPONENTS", contract["component"]),
        "\n",
        f"export const COMPONENT_ORDER = {json.dumps(list(contract['component']), ensure_ascii=False)} as const\n",
        "\n",
        _ts_literal("STAGES", contract["stage"]),
        "\n",
        _ts_literal("STAGE_STATUS_LABELS", contract["stage_status"]),
        "\n",
        _ts_literal("FAULT_CATALOG", contract["fault_catalog"]),
        "\n",
        _ts_literal("FAULT_MATCHERS", contract["fault_matchers"]),
        "\n",
        _ts_literal("FAULT_ATTRIBUTION", contract["fault_attribution"]),
        "\n",
        f"export const FAULT_DEFAULT_CODE = {json.dumps(contract['fault_attribution']['default_code'])} as const\n",
        f"export const ARCHIVE_VERIFY_WAIT_MINUTES = {int(thresholds['archive_verify_wait_minutes'])}\n",
        "\n",
        "export type ScopeKey = keyof typeof SCOPE_LABELS\n",
        "export type ComponentKey = keyof typeof COMPONENTS\n",
        "export type StageKey = keyof typeof STAGES\n",
        "export type FaultCode = keyof typeof FAULT_CATALOG\n",
        "\n",
    ]


def render_python(contract: dict) -> str:
    size = contract["size"]
    thresholds = contract["thresholds"]
    bands = contract["bands"]
    parts = [
        PY_HEADER,
        _py_literal("CONCLUSION_LABELS", contract["conclusion"]),
        "\n",
        _py_literal("CONCLUSION_TONES", contract["conclusion_tone"]),
        "\n",
        _py_literal("PHASE_LABELS", contract["phase"]),
        "\n",
        _py_literal("CONDITION_LABELS", contract["condition"]),
        "\n",
        _py_literal("ARTIFACT_QUALITY_LABELS", contract["artifact_quality"]),
        "\n",
        _py_literal("ARTIFACT_REASON_LABELS", contract["artifact_reason"]),
        "\n",
        _py_literal("NETWORK_GRADE_LABELS", contract["network_grade"]),
        "\n",
        _py_literal("TRANSFER_STAGE_LABELS", contract["transfer_stage"]),
        "\n",
        _py_literal("UPLOAD_MODE_LABELS", contract["upload_mode"]),
        "\n",
        f"SIZE_UNITS = {tuple(size['units'])!r}\n",
        f"SIZE_BASE = {int(size['base'])}\n",
        f"SIZE_ZERO_TEXT = {size['zero']!r}\n",
        f"SIZE_MISSING_TEXT = {size['missing']!r}\n",
        f"SIZE_DIGITS_LT_10 = {int(size['digits_lt_10'])}\n",
        f"SIZE_DIGITS_LT_100 = {int(size['digits_lt_100'])}\n",
        f"SIZE_DIGITS_GTE_100 = {int(size['digits_gte_100'])}\n",
        "\n",
        _py_literal("THRESHOLDS", thresholds),
        "\n",
        _py_literal("PROGRESS_BANDS", bands),
        "\n",
        "MIN_ARCHIVE_BYTES = THRESHOLDS[\"min_archive_bytes\"]\n",
        "MAX_PROCESS_LOG_FILE_BYTES = THRESHOLDS[\"max_process_log_file_bytes\"]\n",
        "MAX_PROCESS_LOG_TASK_BYTES = THRESHOLDS[\"max_process_log_task_bytes\"]\n",
        "STALL_SECONDS = THRESHOLDS[\"stall_seconds\"]\n",
        "COLLECT_BAND_END = PROGRESS_BANDS[\"collect_end\"]\n",
        "DELIVERY_BAND_END = PROGRESS_BANDS[\"delivery_end\"]\n",
        "\n",
        *_extra_python(contract, thresholds),
        PY_FOOTER,
    ]
    return "".join(parts)


def render_ts(contract: dict) -> str:
    size = contract["size"]
    thresholds = contract["thresholds"]
    bands = contract["bands"]
    parts = [
        TS_HEADER,
        _ts_literal("CONCLUSION_LABELS", contract["conclusion"]),
        "\n",
        _ts_literal("CONCLUSION_TONES", contract["conclusion_tone"]),
        "\n",
        _ts_literal("PHASE_LABELS", contract["phase"]),
        "\n",
        _ts_literal("CONDITION_LABELS", contract["condition"]),
        "\n",
        _ts_literal("ARTIFACT_QUALITY_LABELS", contract["artifact_quality"]),
        "\n",
        _ts_literal("ARTIFACT_REASON_LABELS", contract["artifact_reason"]),
        "\n",
        _ts_literal("NETWORK_GRADE_LABELS", contract["network_grade"]),
        "\n",
        _ts_literal("TRANSFER_STAGE_LABELS", contract["transfer_stage"]),
        "\n",
        _ts_literal("UPLOAD_MODE_LABELS", contract["upload_mode"]),
        "\n",
        "export type Conclusion = keyof typeof CONCLUSION_LABELS\n",
        "export type Phase = keyof typeof PHASE_LABELS\n",
        "export type Condition = keyof typeof CONDITION_LABELS\n",
        "export type ArtifactQuality = keyof typeof ARTIFACT_QUALITY_LABELS\n",
        "export type ArtifactReason = keyof typeof ARTIFACT_REASON_LABELS\n",
        "export type NetworkGrade = keyof typeof NETWORK_GRADE_LABELS\n",
        "export type TransferStage = keyof typeof TRANSFER_STAGE_LABELS\n",
        "\n",
        f"export const SIZE_UNITS = {json.dumps(size['units'], ensure_ascii=False)} as const\n",
        f"export const SIZE_BASE = {int(size['base'])}\n",
        f"export const SIZE_ZERO_TEXT = {json.dumps(size['zero'], ensure_ascii=False)}\n",
        f"export const SIZE_MISSING_TEXT = {json.dumps(size['missing'], ensure_ascii=False)}\n",
        f"export const SIZE_DIGITS_LT_10 = {int(size['digits_lt_10'])}\n",
        f"export const SIZE_DIGITS_LT_100 = {int(size['digits_lt_100'])}\n",
        f"export const SIZE_DIGITS_GTE_100 = {int(size['digits_gte_100'])}\n",
        "\n",
        _ts_literal("THRESHOLDS", thresholds),
        "\n",
        _ts_literal("PROGRESS_BANDS", bands),
        "\n",
        f"export const MIN_ARCHIVE_BYTES = {int(thresholds['min_archive_bytes'])}\n",
        f"export const MAX_PROCESS_LOG_FILE_BYTES = {int(thresholds['max_process_log_file_bytes'])}\n",
        f"export const MAX_PROCESS_LOG_TASK_BYTES = {int(thresholds['max_process_log_task_bytes'])}\n",
        f"export const STALL_SECONDS = {int(thresholds['stall_seconds'])}\n",
        f"export const COLLECT_BAND_END = {int(bands['collect_end'])}\n",
        f"export const DELIVERY_BAND_END = {int(bands['delivery_end'])}\n",
        "\n",
        *_extra_ts(contract, thresholds),
        TS_FOOTER,
    ]
    return "".join(parts)


def render_error_catalog(contract: dict) -> str:
    """运行时字典生成物：保持原 JSON 结构，Go 侧双向校验测试继续读取。"""
    errors = [
        {
            "code": code,
            "layer": item["layer"],
            "stage": item["business_stage"],
            "title_zh": item["title_zh"],
            "owner": item["owner"],
            "retryable": item["retryable"],
            "http_status": item["http_status"],
            "user_hint": item["action"],
            "ops_hint": item["admin_detail"],
        }
        for code, item in contract["fault_catalog"].items()
        if item.get("kind") == "runtime"
    ]
    payload = {
        "schema_version": 1,
        "_generated_from": "contracts/log_collect_display.json",
        "domain": "log_collect",
        "naming": "LC-<LAYER>-<NNN>",
        "conventions": contract["conventions"],
        "layers": contract["layers"],
        "errors": errors,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_hints(contract: dict) -> str:
    """客户诊断卡建议表：导出接口不变，内容全部来自契约。"""
    rows = []
    for code, item in sorted(contract["fault_catalog"].items()):
        if item.get("kind") != "runtime":
            continue
        rows.append(
            f"  {json.dumps(code)}: {{\n"
            f"    title: {json.dumps(item['title_zh'], ensure_ascii=False)},\n"
            f"    check: {json.dumps(item['check'], ensure_ascii=False)},\n"
            f"    fix: {json.dumps(item['fix'], ensure_ascii=False)},\n"
            f"  }},"
        )
    return (
        "/** 自动生成，请勿手改。源：contracts/log_collect_display.json */\n"
        "export interface LogCollectErrorHint {\n"
        "  title: string\n"
        "  check: string[]\n"
        "  fix: string[]\n"
        "}\n\n"
        "const HINTS: Record<string, LogCollectErrorHint> = {\n"
        + "\n".join(rows)
        + "\n}\n\n"
        "/** 按错误码取建议；未知码返回 null，由界面指向任务详情。 */\n"
        "export function logCollectErrorHint(code?: string | null): LogCollectErrorHint | null {\n"
        "  const key = String(code || '').trim().toUpperCase()\n"
        "  if (!key) return null\n"
        "  return HINTS[key] || null\n"
        "}\n"
    )


def render_ui(contract: dict) -> str:
    """所有非码展示文案与呈现策略：custom-ui / admin-ui / verify 页共用。"""
    ui = contract["ui"]
    obs = contract["observability"]
    parts = [
        "/** 自动生成，请勿手改。源：contracts/log_collect_display.json */\n",
        _ts_literal("UI_TEXT", ui),
        "\n",
        _ts_literal("STATUS_TEXT", ui["status_text"]),
        _ts_literal("DELIVERY_TEXT", ui["delivery_text"]),
        _ts_literal("MODE_TEXT", ui["mode_text"]),
        _ts_literal("BUCKET_LABELS", ui["bucket_labels"]),
        _ts_literal("MISSING_DATA_TEXT", ui["missing_data"]),
        _ts_literal("UI_FALLBACK", ui["fallback"]),
        _ts_literal("RENDER_POLICY", ui["render_policy"]),
        _ts_literal("OBSERVABILITY_CONTRACT", obs),
        "\n",
    ]
    return "".join(parts)


def render_rules(contract: dict) -> str:
    """有序补码规则：替换 log_collect_error_attribution.py 里手写的 _RULES。"""
    rows = "\n".join(
        f"    ({json.dumps(rule['needle'], ensure_ascii=False)}, "
        f"{json.dumps(rule['code'])}),"
        for rule in contract["attribution_rules"]
    )
    return (
        '"""自动生成，请勿手改。源：contracts/log_collect_display.json。\n\n'
        "顺序即优先级：具体到泛化，新增规则必须想清楚会不会抢走更具体的语义。\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "ATTRIBUTION_RULES: tuple[tuple[str, str], ...] = (\n"
        + rows
        + "\n)\n"
    )


def render_go(contract: dict) -> str:
    """桥侧码 → 中文文案/元数据表。常量仍由 log_collect_errors.go 定义，避免重复声明。"""
    rows = []
    for code, item in sorted(contract["fault_catalog"].items()):
        if item.get("kind") != "runtime":
            continue
        # gofmt 会把 struct 字面量字段按最长字段名对齐；生成器必须直接产出
        # gofmt 格式，否则 CI 的 `test -z "$(gofmt -l .)"` 会红，而 CI 不保证装有 Go。
        rows.append(
            f"\t{json.dumps(code)}: {{\n"
            f"\t\tTitle:     {json.dumps(item['title_zh'], ensure_ascii=False)},\n"
            f"\t\tProblem:   {json.dumps(item['problem'], ensure_ascii=False)},\n"
            f"\t\tAction:    {json.dumps(item['action'], ensure_ascii=False)},\n"
            f"\t\tLayer:     {json.dumps(item['layer'])},\n"
            f"\t\tComponent: {json.dumps(item['component'])},\n"
            f"\t\tStage:     {json.dumps(item['stage'])},\n"
            f"\t}},"
        )
    # 桥侧聚合句同样来自契约。用 init() 逐条赋值而不是 map 字面量，
    # 避免 gofmt 按最长键对齐列导致生成物漂移（CI 不保证装有 Go）。
    text_rows = [
        f"\tcontractText[{json.dumps(key)}] = {json.dumps(value, ensure_ascii=False)}"
        for key, value in sorted(contract["ui"]["go"].items())
    ]
    return (
        "// Code generated by scripts/gen_log_collect_display.py; DO NOT EDIT.\n"
        "// 源：contracts/log_collect_display.json；中文失败文案只在这张表里出现。\n"
        "package main\n\n"
        'import "strings"\n\n'
        "type contractError struct {\n"
        "\tTitle     string\n"
        "\tProblem   string\n"
        "\tAction    string\n"
        "\tLayer     string\n"
        "\tComponent string\n"
        "\tStage     string\n"
        "}\n\n"
        "var contractErrors = map[string]contractError{\n"
        + "\n".join(rows)
        + "\n}\n\n"
        "// contractTitle 返回该码对外展示的一句话结论；未知码返回空串，由调用方按契约兜底。\n"
        "func contractTitle(code string) string {\n"
        "\tif item, ok := contractErrors[code]; ok {\n"
        "\t\treturn item.Title\n"
        "\t}\n"
        "\treturn \"\"\n"
        "}\n\n"
        "// contractText 存放桥侧所有对外文案模板，占位符由 contractFormat 替换。\n"
        "var contractText = map[string]string{}\n\n"
        "func init() {\n"
        + "\n".join(text_rows)
        + "\n}\n\n"
        "// contractFormat 按契约占位符渲染文案，桥侧不再手写中文。\n"
        "func contractFormat(key string, vars map[string]string) string {\n"
        "\ttpl := contractText[key]\n"
        "\tfor name, value := range vars {\n"
        '\t\ttpl = strings.ReplaceAll(tpl, "{"+name+"}", value)\n'
        "\t}\n"
        "\treturn tpl\n"
        "}\n"
    )


COMPONENT_CODE_PREFIX = {
    "hci": "LC-HCI-",
    "acli": "LC-ACLI-",
    "bridge": "LC-BRIDGE-",
    "htp": "LC-HTP-",
    "log_server": "LC-5888-",
}

FAULT_DETAIL_FIELDS = (
    "code", "scope", "scope_label", "component", "component_label", "component_tag",
    "stage", "stage_label", "problem", "action", "customer_text", "admin_detail",
    "evidence", "trace_id",
)


def validate_contract(contract: dict) -> None:
    """对抗性校验：契约内部引用必须闭合。

    防止生成出「悬空错误码」「前缀骗人」「跨段阶段不可展示」的常量；
    这类错误一旦进产品，客户看到的一眼定界反而是一眼误导。
    """
    scopes = contract["scope"]
    components = contract["component"]
    stages = contract["stage"]
    catalog = contract["fault_catalog"]
    matchers = contract["fault_matchers"]
    attribution = contract["fault_attribution"]
    problems: list[str] = []

    for key, item in components.items():
        if item["scope"] not in scopes:
            problems.append(f"component.{key}.scope 含非枚举段：{item['scope']}")
    for key, item in stages.items():
        for scope in item["scopes"]:
            if scope not in scopes:
                problems.append(f"stage.{key}.scopes 含非枚举段：{scope}")
        for component in item["components"]:
            if component not in components:
                problems.append(f"stage.{key}.components 含非枚举组件：{component}")

    if tuple(stages) != tuple(attribution["stage_order"]):
        problems.append("fault_attribution.stage_order 与 stage 定义顺序不一致")
    if tuple(attribution["fields"]) != FAULT_DETAIL_FIELDS:
        problems.append("fault_attribution.fields 与 fault_detail() 返回字段不一致")
    if attribution["default_code"] not in catalog:
        problems.append("default_code 不在 fault_catalog 中")

    matcher_codes = {item["code"] for item in matchers}
    for matcher in matchers:
        if not matcher["keywords"]:
            problems.append(f"{matcher['code']} 没有关键词")
        if matcher["code"] not in catalog:
            problems.append(f"fault_matchers 引用了不存在的错误码：{matcher['code']}")

    for code, item in catalog.items():
        kind = item.get("kind", "signal")
        for field in ("component", "stage", "problem", "action", "tone", "admin_detail", "evidence"):
            if not item.get(field):
                problems.append(f"{code} 缺少字段：{field}")
        # runtime 码由 bridge 直接出码，前缀是层名（LC-ENV-…）而不是组件名；
        # 「前缀绑定组件」只对 signal（证据派生归因码）成立，否则 71 个码全会被误拒。
        if kind == "signal":
            prefix = COMPONENT_CODE_PREFIX.get(item["component"])
            if prefix and not code.startswith(prefix):
                problems.append(f"{code} 前缀与责任组件 {item['component']} 不一致，应为 {prefix}")
        else:
            for field in ("layer", "scope", "title_zh", "check", "fix", "match_keywords"):
                if not item.get(field):
                    problems.append(f"{code} 缺少运行时字段：{field}")
        if item["component"] not in components:
            problems.append(f"{code}.component 非枚举组件：{item['component']}")
        if item["stage"] not in stages:
            problems.append(f"{code}.stage 不存在：{item['stage']}")
        # runtime 码不需要关键词：谁失败谁出码。只有证据派生的 signal 码必须从文本归类。
        if kind == "signal" and code != attribution["default_code"] and code not in matcher_codes:
            problems.append(f"{code} 没有匹配规则，界面永远无法归类到该错误")

    # 别名必须指向存在的码、无自环；客户不许看到两个不同的码描述同一件事。
    for alias in contract.get("aliases", []):
        if alias["from"] == alias["to"]:
            problems.append(f"别名自环：{alias['from']}")
        if alias["to"] not in catalog:
            problems.append(f"别名目标不存在：{alias['to']}")

    # 呈现契约：原文/码/无数据提示在同一视图内不得重复；禁词不得进入任何文案。
    ui = contract.get("ui") or {}
    policy = ui.get("render_policy") or {}
    for key, limit in (("error_message_max_per_view", 1), ("error_code_max_per_view", 1),
                       ("bridge_outdated_max_per_view", 1)):
        if policy.get(key, 0) != limit:
            problems.append(f"ui.render_policy.{key} 必须为 {limit}")
    forbidden = (contract.get("observability") or {}).get("forbidden_words", [])
    text_blob = json.dumps({"ui": ui, "codes": catalog}, ensure_ascii=False)
    for word in forbidden:
        if word in text_blob:
            problems.append(f"契约文案含禁词：{word}")
    if not contract.get("attribution_rules"):
        problems.append("attribution_rules 为空：无码历史任务将无法补码")
    for rule in contract.get("attribution_rules", []):
        if rule["code"] not in catalog:
            problems.append(f"补码规则引用不存在的码：{rule['code']}")

    if problems:
        raise ValueError("契约校验失败：\n  - " + "\n  - ".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="仅校验产物是否与契约一致")
    args = parser.parse_args()

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    try:
        validate_contract(contract)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    outputs = {
        PY_OUT: render_python(contract),
        TS_OUT: render_ts(contract),
        CATALOG_OUT: render_error_catalog(contract),
        HINTS_OUT: render_hints(contract),
        UI_OUT: render_ui(contract),
        RULES_OUT: render_rules(contract),
        GO_OUT: render_go(contract),
    }

    drifted = []
    for path, content in outputs.items():
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != content:
                drifted.append(str(path.relative_to(REPO_ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")

    if args.check and drifted:
        print("契约生成物与源不一致，请运行 scripts/gen_log_collect_display.py：", file=sys.stderr)
        for item in drifted:
            print(f"  - {item}", file=sys.stderr)
        return 1
    if args.check:
        print("契约生成物一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
