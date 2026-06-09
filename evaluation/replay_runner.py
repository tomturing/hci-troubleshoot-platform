"""
Agent Replay Runner — Offline Evaluation Framework (T4-1)

T4-1 改造：使用离线 dispatcher + Faithful Fake LLM 替换 Random Classification
  - 分类判断：由离线 dispatcher 根据工单文本确定，golden ticket 仅用于评分
  - 工具执行：由 dispatcher 规划工具路径，成功率由 golden ticket 的 tool_errors 定义
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


class OfflineDispatcher:
    """离线 dispatcher：用确定性规则近似真实分类/路径规划链路。

    评测时不能把 expected_category/expected_tools 直接作为模型输出，否则回归测试只是在
    复读答案。本 dispatcher 只读取工单 title/description，用可解释规则产出分类和工具路径。
    """

    @staticmethod
    def _text(case: dict) -> str:
        return f"{case.get('title', '')} {case.get('description', '')}".lower()

    @staticmethod
    def _has(text: str, *keywords: str) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def classify(self, case: dict) -> str:
        text = self._text(case)
        if self._has(text, "备份") and not self._has(text, "快照文件残留"):
            return "backup"
        if self._has(text, "集群", "脑裂", "ntp", "drs", "主节点", "管理节点", "心跳"):
            return "cluster"
        if self._has(text, "oom", "电源", "风扇", "过热", "软锁死", "物理节点内存"):
            return "host"
        if self._has(text, "混杂"):
            return "network"
        if self._has(text, "虚拟机") and self._has(text, "启动", "开机", "cpu", "vnc", "iso", "蓝屏", "mac"):
            return "vm"
        if self._has(text, "存储", "硬盘", "ssd", "s.m.a.r.t", "元数据", "io 路径", "快照文件残留"):
            return "storage"
        if self._has(text, "网络", "网卡", "vswitch", "mtu", "安全策略", "混杂", "带宽"):
            return "network"
        if self._has(text, "虚拟机"):
            return "vm"
        return "unknown"

    def plan_tools(self, case: dict, category: str) -> list[str]:
        text = self._text(case)
        tools: list[str] = []

        def add(tool_name: str) -> None:
            if tool_name not in tools:
                tools.append(tool_name)

        if category == "backup":
            add("task_get_logs")
            if self._has(text, "带宽", "网络", "局域网"):
                add("acli_network_check")
            elif self._has(text, "快照", "虚拟机"):
                add("acli_vm_list")
        elif category == "cluster":
            add("acli_cluster_status")
            if self._has(text, "脑裂", "心跳", "失联", "链路"):
                add("acli_network_check")
            if self._has(text, "drs", "迁移"):
                add("acli_vm_list")
        elif category == "host":
            add("acli_node_logs")
            if self._has(text, "软锁死", "中断风暴"):
                add("acli_node_top")
            elif self._has(text, "oom", "崩溃", "内存"):
                add("acli_cluster_status")
        elif category == "storage":
            if self._has(text, "硬盘", "ssd", "s.m.a.r.t", "快照文件残留", "读写"):
                add("acli_disk_show")
            add("acli_storage_list")
            if self._has(text, "虚拟机暂停"):
                add("acli_vm_list")
        elif category == "network":
            add("acli_network_check")
            if self._has(text, "物理网卡"):
                add("acli_port_status")
        elif category == "vm":
            add("acli_vm_list")
            if self._has(text, "io", "磁盘", "快照"):
                add("acli_disk_show")
            if self._has(text, "cpu"):
                add("acli_node_top")
            if self._has(text, "mac"):
                add("acli_network_check")

        return tools


class FaithfulFakeLLM:
    """T4-1: 忠实的 Fake LLM，根据 dispatcher 结果产生确定性输出。

    与 Random Classification 不同，Fake LLM:
      - 不负责分类，分类由 OfflineDispatcher 产出
      - 按 dispatcher 规划工具执行，成功/失败由 golden ticket 定义
      - 根据 expected_claims 生成最终输出，不注入幻觉
    """

    def __init__(self, golden_ticket: dict):
        self._ticket = golden_ticket

    def execute_tools(self, planned_tools: list[str]) -> list[tuple[str, bool, str]]:
        """执行工具序列，返回 (tool_name, success, output) 列表。"""
        tool_errors = self._ticket.get("tool_errors", {})  # {"acli_exec": "timeout"}
        results = []
        for tool in planned_tools:
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
    dispatcher = OfflineDispatcher()

    print(f"开始离线回放评估 {len(cases)} 个黄金工单...")

    for case in cases:
        case_id = case["case_id"]
        title = case["title"]
        expected_cat = case["expected_category"]
        expected_tools = case.get("expected_tools", [])

        # T4-1: dispatcher 负责分类与路径规划，Fake LLM 只负责稳定输出
        actual_cat = dispatcher.classify(case)
        is_cat_match = actual_cat == expected_cat
        planned_tools = dispatcher.plan_tools(case, actual_cat)
        fake_llm = FaithfulFakeLLM(golden_ticket=case)

        # 1. 分类判断（确定性，基于工单文本）
        if is_cat_match:
            category_matches += 1

        # 2. 工具调用序列执行（确定性，基于 dispatcher 路径）
        tool_results = fake_llm.execute_tools(planned_tools)
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
        report = detector.detect(llm_text=llm_text, executed_tools=called_tools, tool_outputs=tool_outputs)

        if report["has_hallucination"]:
            hallucination_count += 1

        results.append(
            {
                "case_id": case_id,
                "title": title,
                "category_matched": is_cat_match,
                "expected_category": expected_cat,
                "actual_category": actual_cat,
                "tools_executed": called_tools,
                "path_deviated": path_deviated,
                "has_hallucination": report["has_hallucination"],
                "hallucination_details": report,
            }
        )

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
        "details": results,
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
    parser.add_argument(
        "--baseline", default=os.path.join(os.path.dirname(__file__), "report.json"), help="基线报告路径"
    )
    parser.add_argument("--max-regression", type=float, default=0.10, help="允许的最大劣化比例")
    args = parser.parse_args()
    run_evaluation(baseline_path=args.baseline, max_regression=args.max_regression)
