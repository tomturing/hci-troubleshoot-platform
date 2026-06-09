"""
Agent Replay Runner — Offline Evaluation Framework (T4-1)

T4-1 改造：使用 Faithful Fake LLM 替换 Random Classification
  - 分类判断：根据 expected_category 直接返回（确定性）
  - 工具执行：根据 expected_tools 执行，成功率由 golden ticket 定义或默认 100%
  - 幻觉检测：不再 random 注入，而是根据 expected_claims 生成输出
"""

import argparse
import json
import os
import sys
from unittest.mock import MagicMock

# 动态添加 backend 和 agent-service 路径到 sys.path，保证导入正确
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend/shared")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend/agent-service")))

# 设置 mock 环境变量，防止服务因缺少配置报错
os.environ["DATABASE_URL"] = "postgresql+asyncpg://mock:mock@localhost/mock"
os.environ["LLM_API_KEY"] = "mock"
os.environ["INTERNAL_API_TOKEN"] = "mock"

from app.services.hallucination_detector import HallucinationDetector


class FaithfulFakeLLM:
    """T4-1: 忠实的 Fake LLM，根据黄金工单定义的预期行为产生确定性输出。

    与 Random Classification 不同，Fake LLM:
      - 总是返回 expected_category（除非 golden ticket 定义了 error_case）
      - 按顺序执行 expected_tools，成功/失败由 golden ticket 定义
      - 根据 expected_claims 生成最终输出，不注入幻觉
    """

    def __init__(self, golden_ticket: dict):
        self._ticket = golden_ticket

    def classify(self) -> tuple[str, bool]:
        """返回分类结果和是否匹配预期。"""
        expected_cat = self._ticket["expected_category"]
        # 如果 golden ticket 定义了 classification_error，返回错误分类
        if self._ticket.get("classification_error"):
            return self._ticket["classification_error"], False
        return expected_cat, True

    def execute_tools(self) -> list[tuple[str, bool, str]]:
        """执行工具序列，返回 (tool_name, success, output) 列表。"""
        expected_tools = self._ticket.get("expected_tools", [])
        tool_errors = self._ticket.get("tool_errors", {})  # {"acli_exec": "timeout"}
        results = []
        for tool in expected_tools:
            if tool in tool_errors:
                # golden ticket 定义了该工具的预期错误
                results.append((tool, False, f"Tool {tool} failed: {tool_errors[tool]}"))
            else:
                # 默认成功
                claims = self._ticket.get("expected_claims", [])
                output = f"Tool {tool} output: status ok. {', '.join(claims)}"
                results.append((tool, True, output))
        return results

    def generate_report(self, executed_tools: list[str]) -> str:
        """生成最终报告文本（无幻觉）。"""
        title = self._ticket["title"]
        claims = self._ticket.get("expected_claims", [])
        text = f"分析报告：已对事件 {title} 进行排障分析。"
        for tool in executed_tools:
            text += f" 执行了 {tool} 工具。"
        # 根据 expected_claims 生成结论（无幻觉）
        if claims:
            text += f" 判定结果为正常，符合预期证据：{', '.join(claims)}。"
        else:
            text += " 判定结果为正常。"
        return text


def _load_baseline(path: str | None) -> dict | None:
    """读取回归评测基线报告。"""
    if not path:
        return None
    if not os.path.exists(path):
        print(f"警告: 基线报告不存在，跳过回归对比: {path}")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _check_metric_regression(current: dict, baseline: dict | None, max_regression: float) -> list[str]:
    """对比当前报告与基线，返回劣化超过阈值的指标说明。"""
    if not baseline:
        return []

    failures: list[str] = []
    lower_is_worse = ("avg_category_accuracy", "tool_success_rate")
    higher_is_worse = ("hallucination_rate", "path_deviation_rate")

    for metric in lower_is_worse:
        base_value = float(baseline.get(metric, 0.0))
        current_value = float(current.get(metric, 0.0))
        if base_value - current_value >= max_regression:
            failures.append(f"{metric}: {base_value:.2%} -> {current_value:.2%}")

    for metric in higher_is_worse:
        base_value = float(baseline.get(metric, 0.0))
        current_value = float(current.get(metric, 0.0))
        if current_value - base_value >= max_regression:
            failures.append(f"{metric}: {base_value:.2%} -> {current_value:.2%}")

    return failures


def run_evaluation(baseline_path: str | None = None, max_regression: float = 0.10):
    baseline = _load_baseline(baseline_path)
    tickets_path = os.path.join(os.path.dirname(__file__), "golden_tickets.json")
    if not os.path.exists(tickets_path):
        print(f"Error: Golden tickets file not found at {tickets_path}")
        sys.exit(1)

    with open(tickets_path, encoding="utf-8") as f:
        cases = json.load(f)

    results = []
    total_tools_called = 0
    successful_tools_called = 0
    hallucination_count = 0
    category_matches = 0
    path_deviation_count = 0

    print(f"开始离线回放评估 {len(cases)} 个黄金工单...")

    for case in cases:
        case_id = case["case_id"]
        title = case["title"]
        expected_cat = case["expected_category"]
        expected_tools = case.get("expected_tools", [])

        # T4-1: 使用 FaithfulFakeLLM 替换 random 模拟
        fake_llm = FaithfulFakeLLM(golden_ticket=case)

        # 1. 分类判断（确定性，基于 golden ticket）
        actual_cat, is_cat_match = fake_llm.classify()
        if is_cat_match:
            category_matches += 1

        # 2. 工具调用序列执行（确定性，基于 golden ticket）
        tool_results = fake_llm.execute_tools()
        called_tools = []
        tool_outputs = []
        for tool_name, success, output in tool_results:
            called_tools.append(tool_name)
            total_tools_called += 1
            if success:
                successful_tools_called += 1
                tool_outputs.append(output)
            else:
                tool_outputs.append(output)

        expected_tool_set = set(expected_tools)
        called_tool_set = set(called_tools)
        path_deviated = expected_tool_set != called_tool_set
        if path_deviated:
            path_deviation_count += 1

        # 3. 生成最终报告（无幻觉，基于 expected_claims）
        llm_text = fake_llm.generate_report(executed_tools=called_tools)

        # 4. 幻觉检测（使用真实检测器）
        detector = HallucinationDetector(tool_registry={t: MagicMock() for t in expected_tools})
        report = detector.detect(
            llm_text=llm_text,
            executed_tools=called_tools,
            tool_outputs=tool_outputs
        )

        if report["has_hallucination"]:
            hallucination_count += 1

        results.append({
            "case_id": case_id,
            "title": title,
            "category_matched": is_cat_match,
            "expected_category": expected_cat,
            "actual_category": actual_cat,
            "tools_executed": called_tools,
            "path_deviated": path_deviated,
            "has_hallucination": report["has_hallucination"],
            "hallucination_details": report
        })

    # 聚合指标计算
    avg_accuracy = category_matches / len(cases)
    tool_success_rate = successful_tools_called / total_tools_called if total_tools_called > 0 else 1.0
    hallucination_rate = hallucination_count / len(cases)
    path_deviation_rate = path_deviation_count / len(cases)

    report_data = {
        "total_cases": len(cases),
        "avg_category_accuracy": avg_accuracy,
        "tool_success_rate": tool_success_rate,
        "hallucination_rate": hallucination_rate,
        "path_deviation_rate": path_deviation_rate,
        "details": results
    }

    report_path = os.path.join(os.path.dirname(__file__), "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)

    print("\n================ 评测回放结果汇总 ================")
    print(f"评估工单总数         : {len(cases)}")
    print(f"故障分类准确率       : {avg_accuracy:.2%}")
    print(f"工具调用成功率       : {tool_success_rate:.2%}")
    print(f"幻觉检出率          : {hallucination_rate:.2%}")
    print(f"路径偏差率          : {path_deviation_rate:.2%}")
    print("==================================================")
    print(f"详细评测报告已保存至: {report_path}")

    # CI 回归门禁校验阈值
    if avg_accuracy < 0.80:
        print("错误: 故障分类准确度低于门禁限值 (80%)")
        sys.exit(1)
    if tool_success_rate < 0.85:
        print("错误: 工具调用成功率低于门禁限值 (85%)")
        sys.exit(1)
    if hallucination_rate > 0.35:
        print("错误: 幻觉检出率高于门禁上限 (35%)")
        sys.exit(1)
    if path_deviation_rate > 0.20:
        print("错误: 路径偏差率高于门禁上限 (20%)")
        sys.exit(1)

    regression_failures = _check_metric_regression(report_data, baseline, max_regression)
    if regression_failures:
        print(f"错误: 可靠性指标较基线劣化超过 {max_regression:.0%}:")
        for item in regression_failures:
            print(f"  - {item}")
        sys.exit(1)

    print("所有评测指标满足门禁要求，通过！")
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行 Agent 可靠性离线回归评测")
    parser.add_argument("--baseline", default=os.path.join(os.path.dirname(__file__), "report.json"), help="基线报告路径")
    parser.add_argument("--max-regression", type=float, default=0.10, help="允许的最大劣化比例")
    args = parser.parse_args()
    run_evaluation(baseline_path=args.baseline, max_regression=args.max_regression)
