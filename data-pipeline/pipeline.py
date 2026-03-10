"""
pipeline.py — ETL 串联调度器

功能：
  - 串联 fetcher → converter → enricher → ingestor 四个阶段
  - 支持 --stage 参数选择单阶段或全链路执行
  - 支持 --limit 小批量快速测试
  - 统一输出各阶段耗时和处理数量

使用：
  # 全链路（小批量测试）
  uv run data-pipeline/pipeline.py --stage all --limit 50

  # 只运行特定阶段
  uv run data-pipeline/pipeline.py --stage fetch --limit 100
  uv run data-pipeline/pipeline.py --stage convert
  uv run data-pipeline/pipeline.py --stage enrich --llm
  uv run data-pipeline/pipeline.py --stage ingest

  # 全量运行（tmux 后台建议）
  uv run data-pipeline/pipeline.py --stage all

环境变量：
  KB_URL      KB Service 地址（默认 http://localhost:8004）
  KB_TOKEN    认证 Token（默认 hci-dev-internal-token）
  ZAI_API_KEY 用于 enricher --llm 模式（可选）
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Callable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline")

# ─────────────────────────────────────────────────────────────
# 阶段定义
# ─────────────────────────────────────────────────────────────
STAGES = ["fetch", "convert", "enrich", "ingest"]


def _run_stage(
    name: str,
    func: Callable,
    kwargs: dict,
) -> dict:
    """运行单个阶段，返回 {"stage": name, "result": ..., "elapsed": float}"""
    logger.info("─────── Stage: %s 开始 ───────", name.upper())
    t0 = time.time()
    try:
        result = func(**kwargs)
    except Exception as exc:
        logger.exception("Stage %s 异常: %s", name, exc)
        result = {"error": str(exc)}
    elapsed = time.time() - t0
    logger.info("─────── Stage: %s 完成（%.1fs）───────", name.upper(), elapsed)
    return {"stage": name, "result": result, "elapsed": elapsed}


# ─────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────
def run(
    stage: str = "all",
    limit: int | None = None,
    use_llm: bool = False,
) -> list[dict]:
    """
    执行 ETL 流程。

    Args:
        stage: "all" | "fetch" | "convert" | "enrich" | "ingest"
        limit: 各阶段最多处理 N 条
        use_llm: enricher 是否启用 LLM 打标

    Returns:
        各阶段执行结果列表
    """
    # 延迟导入，避免在只运行部分阶段时加载不必要的模块
    import fetcher
    import converter
    import enricher
    import ingestor

    stage_map = {
        "fetch": (fetcher.run, {"limit": limit}),
        "convert": (converter.run, {"limit": limit}),
        "enrich": (enricher.run, {"limit": limit, "use_llm": use_llm}),
        "ingest": (ingestor.run, {"limit": limit}),
    }

    stages_to_run = STAGES if stage == "all" else [stage]
    results = []

    for s in stages_to_run:
        if s not in stage_map:
            logger.error("未知阶段: %s", s)
            continue
        func, kwargs = stage_map[s]
        r = _run_stage(s, func, kwargs)
        results.append(r)

    _print_summary(results)
    return results


def _print_summary(results: list[dict]) -> None:
    """打印执行汇总"""
    print("\n" + "═" * 50)
    print("ETL PIPELINE 汇总")
    print("═" * 50)
    total_elapsed = 0.0
    for r in results:
        stage = r["stage"].upper()
        elapsed = r["elapsed"]
        total_elapsed += elapsed
        result = r.get("result", {})
        error = result.get("error") if isinstance(result, dict) else None
        if error:
            print(f"  {stage:<10} ❌  {error}  ({elapsed:.1f}s)")
        elif isinstance(result, int):
            print(f"  {stage:<10} ✅  新增 {result} 篇  ({elapsed:.1f}s)")
        elif isinstance(result, dict):
            summary = " | ".join(f"{k}={v}" for k, v in result.items() if not k.startswith("_"))
            print(f"  {stage:<10} ✅  {summary}  ({elapsed:.1f}s)")
        else:
            print(f"  {stage:<10} ✅  {elapsed:.1f}s")
    print(f"  {'总计':<10}     {total_elapsed:.1f}s")
    print("═" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL 串联调度器")
    parser.add_argument(
        "--stage",
        choices=["all", "fetch", "convert", "enrich", "ingest"],
        default="all",
        help="执行阶段（默认 all）",
    )
    parser.add_argument("--limit", type=int, default=None, help="各阶段最多处理 N 条")
    parser.add_argument("--llm", dest="use_llm", action="store_true", help="enricher 启用 LLM 打标")
    args = parser.parse_args()

    run(stage=args.stage, limit=args.limit, use_llm=args.use_llm)


if __name__ == "__main__":
    main()
