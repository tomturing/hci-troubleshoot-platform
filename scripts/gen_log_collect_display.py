#!/usr/bin/env python3
"""从 contracts/log_collect_display.json 生成前后端共用常量。

单一来源：契约 JSON。产物：Python 常量 + TS 常量。
用法：
    python3 scripts/gen_log_collect_display.py           # 写入产物
    python3 scripts/gen_log_collect_display.py --check   # 仅校验，漂移则退出码 1

为什么不手写两份：状态文案、体积格式化、阈值一旦两侧各写一份，必然漂移
（历史上同一个 608 字节包出现过 608 B / 0.0 MB / 608 字节三种口径）。
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
"""


def _py_literal(name: str, value: object) -> str:
    return f"{name} = {json.dumps(value, ensure_ascii=False, indent=4, sort_keys=True)}\n"


def _ts_literal(name: str, value: object) -> str:
    return f"export const {name} = {json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)} as const\n"


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
        TS_FOOTER,
    ]
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="仅校验产物是否与契约一致")
    args = parser.parse_args()

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    outputs = {
        PY_OUT: render_python(contract),
        TS_OUT: render_ts(contract),
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
