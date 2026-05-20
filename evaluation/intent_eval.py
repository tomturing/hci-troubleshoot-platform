#!/usr/bin/env python3
"""意图识别评估脚本 - 支持批量创建工单并触发AI意图识别"""

import argparse
import asyncio
import functools
import json
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

# 强制刷新输出
print = functools.partial(print, flush=True)

import httpx
import pandas as pd

# 全局标志：是否收到中断信号
_interrupted = False


def signal_handler(sig, frame):
    """处理 Ctrl+C 中断信号"""
    global _interrupted
    print("\n\n⚠️ 收到中断信号，正在保存进度...")
    _interrupted = True


signal.signal(signal.SIGINT, signal_handler)


class Config:
    """配置类"""
    API_BASE_URL = "http://172.22.73.249"
    ASSISTANT_TYPE = "glm-5"
    TIMEOUT = 120.0
    SAVE_INTERVAL = 10  # 每处理多少条保存一次进度


class IntentEvaluation:
    """意图识别评估类"""

    def __init__(self, api_base_url: str = Config.API_BASE_URL):
        self.api_base_url = api_base_url
        self.client_id = f"eval-{uuid.uuid4().hex[:8]}"

    async def create_case(self, title: str, description: str) -> dict:
        """创建工单"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.api_base_url}/api/cases/",
                json={"client_id": self.client_id, "title": title, "description": description},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def create_conversation(self, case_id: str, assistant_type: str = Config.ASSISTANT_TYPE) -> dict:
        """创建对话"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.api_base_url}/api/conversations/",
                params={"case_id": case_id, "assistant_type": assistant_type},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def send_message_and_wait(self, conversation_id: str, case_id: str, content: str) -> str:
        """发送消息并等待完整 SSE 流"""
        ai_content = []
        error_msg = None

        async with httpx.AsyncClient(timeout=Config.TIMEOUT) as client, client.stream(
            "POST",
            f"{self.api_base_url}/api/conversations/{conversation_id}/message",
            json={"case_id": case_id, "role": "user", "content": content},
        ) as response:
            # 检查 HTTP 状态码
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"

            async for line in response.aiter_lines():
                # 处理 SSE 事件行
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    if event_type == "error":
                        continue

                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        if "error" in chunk or "code" in chunk:
                            error_msg = chunk.get("message", chunk.get("error", str(chunk)))
                        elif "content" in chunk:
                            ai_content.append(chunk["content"])
                    except json.JSONDecodeError:
                        pass

        if error_msg and not ai_content:
            raise Exception(error_msg)

        return "".join(ai_content)

    async def process_record(self, problem_id: str, problem_desc: str, index: int, assistant_type: str = Config.ASSISTANT_TYPE) -> dict:
        """处理单条记录"""
        title = f"[{problem_id}] {problem_desc}"
        if len(title) > 200:
            title = title[:197] + "..."

        case = await self.create_case(title=title, description=problem_desc)
        case_id = case["case_id"]

        conv = await self.create_conversation(case_id=case_id, assistant_type=assistant_type)
        conversation_id = conv["conversation_id"]

        ai_response = await self.send_message_and_wait(conversation_id, case_id, problem_desc)

        if not ai_response or len(ai_response) < 10:
            raise Exception(f"AI 响应为空或过短 (长度: {len(ai_response)})")

        return {
            "index": index,
            "problem_id": problem_id,
            "problem_desc": problem_desc,
            "case_id": case_id,
            "conversation_id": conversation_id,
            "assistant_type": assistant_type,
            "ai_response_preview": ai_response[:500] if ai_response else "",
            "status": "success",
            "processed_at": datetime.now().isoformat(),
        }


class EvaluationRunner:
    """评估运行器(支持断点续传)"""

    def __init__(self, excel_path: str, api_base_url: str, assistant_type: str, output_dir: Path):
        self.excel_path = excel_path
        self.api_base_url = api_base_url
        self.assistant_type = assistant_type
        self.output_dir = output_dir

        self.progress_file = output_dir / "progress.json"
        self.results_file = output_dir / "eval_results.json"
        self.failed_file = output_dir / "failed_records.json"

        self.progress = self._load_json(self.progress_file, {"last_index": 0, "total": 0})
        self.results = self._load_json(self.results_file, [])
        self.failed = self._load_json(self.failed_file, [])

        self.stats = {"total": 0, "processed": 0, "success": 0, "failed": 0}

    def _load_json(self, path: Path, default):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return default

    def _save_progress(self, last_index: int, total: int):
        self.progress_file.write_text(
            json.dumps({"last_index": last_index, "total": total, "updated_at": datetime.now().isoformat()}, ensure_ascii=False),
            encoding="utf-8"
        )

    def _save_results(self):
        self.results_file.write_text(json.dumps(self.results, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_failed(self):
        self.failed_file.write_text(json.dumps(self.failed, ensure_ascii=False, indent=2), encoding="utf-8")

    async def run(self) -> dict:
        """运行评估"""
        df = pd.read_excel(self.excel_path)
        total = len(df)
        self.stats["total"] = total

        start_index = self.progress.get("last_index", 0)
        if start_index > 0:
            print(f"\n📦 断点续传: 从第 {start_index + 1} 条继续 (共 {total} 条)")
        else:
            print(f"\n🚀 开始处理: 共 {total} 条记录")

        evaluator = IntentEvaluation(self.api_base_url)

        for i in range(start_index, total):
            if _interrupted:
                self._save_progress(i, total)
                self._save_results()
                self._save_failed()
                print(f"✅ 进度已保存至第 {i} 条, 下次运行将从这里继续")
                break

            row = df.iloc[i]
            problem_id = str(row["问题编号"])
            problem_desc = str(row["问题描述"])

            progress_pct = (i + 1) / total * 100
            print(f"[{i+1}/{total}] ({progress_pct:.1f}%) {problem_id}: {problem_desc[:30]}...")

            try:
                result = await evaluator.process_record(problem_id, problem_desc, i + 1, self.assistant_type)
                self.results.append(result)
                self.stats["success"] += 1

                if (i + 1) % Config.SAVE_INTERVAL == 0:
                    self._save_progress(i + 1, total)
                    self._save_results()
                    print(f"    💾 已保存进度 ({i+1}/{total})")

            except Exception as e:
                print(f"    ❌ 失败: {e}")
                self.failed.append({
                    "index": i + 1,
                    "problem_id": problem_id,
                    "problem_desc": problem_desc,
                    "error": str(e),
                    "failed_at": datetime.now().isoformat(),
                })
                self.stats["failed"] += 1
                self._save_progress(i + 1, total)
                self._save_failed()

            self.stats["processed"] = i + 1

        if not _interrupted:
            self._save_progress(total, total)
            self._save_results()
            self._save_failed()

        return self.stats


def cmd_run(args):
    """执行评估命令"""
    excel_path = Path(args.input)
    api_base_url = args.api or Config.API_BASE_URL
    assistant_type = args.assistant or Config.ASSISTANT_TYPE
    output_dir = Path(args.output_dir) if args.output_dir else Path(__file__).parent

    print("=" * 70)
    print("意图识别评估脚本")
    print("=" * 70)
    print(f"Excel: {excel_path}")
    print(f"API: {api_base_url}")
    print(f"助手类型: {assistant_type}")
    print(f"输出目录: {output_dir}")
    print("=" * 70)

    if not excel_path.exists():
        print(f"❌ Excel 文件不存在: {excel_path}")
        return 1

    try:
        with httpx.Client() as client:
            resp = client.get(f"{api_base_url}/api/cases/all?limit=1", timeout=5.0)
            print("✅ API 服务正常")
    except Exception as e:
        print(f"❌ API 不可达: {e}")
        return 1

    runner = EvaluationRunner(str(excel_path), api_base_url, assistant_type, output_dir)
    stats = asyncio.run(runner.run())

    print("\n" + "=" * 70)
    print("处理完成统计:")
    print(f"  总数: {stats['total']}")
    print(f"  成功: {stats['success']}")
    print(f"  失败: {stats['failed']}")
    print(f"  失败记录: {output_dir / 'failed_records.json'}")
    print(f"  结果文件: {output_dir / 'eval_results.json'}")
    print("=" * 70)
    return 0


def cmd_export(args):
    """导出结果到Excel命令"""
    excel_path = Path(args.input)
    results_path = Path(args.results) if args.results else Path(__file__).parent / "eval_results.json"
    failed_path = results_path.parent / "failed_records.json"
    output_path = Path(args.output) if args.output else excel_path.with_name(excel_path.stem + "-结果.xlsx")
    api_base_url = args.api or Config.API_BASE_URL

    print("=" * 70)
    print("导出AI回复到Excel")
    print("=" * 70)
    print(f"Excel: {excel_path}")
    print(f"结果: {results_path}")
    print(f"输出: {output_path}")
    print("=" * 70)

    if not excel_path.exists():
        print(f"❌ Excel 文件不存在: {excel_path}")
        return 1
    if not results_path.exists():
        print(f"❌ 结果文件不存在: {results_path}")
        return 1

    df = pd.read_excel(excel_path)
    print(f"Excel总行数: {len(df)}")

    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)

    problem_conv = {}
    for r in results:
        if r.get("conversation_id"):
            problem_conv[r["problem_id"]] = r["conversation_id"]

    failed_problems = set()
    if failed_path.exists():
        with open(failed_path, encoding="utf-8") as f:
            failed = json.load(f)
        failed_problems = {f["problem_id"] for f in failed}

    print(f"成功记录: {len(problem_conv)}")
    print(f"失败记录: {len(failed_problems)}")

    ai_responses = []
    with httpx.Client(timeout=10.0) as client:
        for i, row in df.iterrows():
            problem_id = str(row["问题编号"])

            if problem_id in failed_problems:
                ai_responses.append("处理失败")
                continue

            if problem_id not in problem_conv:
                ai_responses.append("")
                continue

            conv_id = problem_conv[problem_id]

            try:
                resp = client.get(f"{api_base_url}/api/conversations/{conv_id}/messages")
                messages = resp.json()

                assistant_content = ""
                for m in messages:
                    if m["role"] == "assistant":
                        assistant_content = m["content"]
                        break

                ai_responses.append(assistant_content)

            except Exception as e:
                ai_responses.append(f"获取失败: {e}")

            if (i + 1) % 100 == 0:
                print(f"  已处理: {i + 1}/{len(df)}")

    df["AI回复内容"] = ai_responses
    df.to_excel(output_path, index=False)
    print(f"\n✅ 结果已保存: {output_path}")
    return 0


def cmd_status(args):
    """查看进度状态命令"""
    output_dir = Path(args.output_dir) if args.output_dir else Path(__file__).parent
    progress_file = output_dir / "progress.json"
    results_file = output_dir / "eval_results.json"
    failed_file = output_dir / "failed_records.json"

    print("=" * 70)
    print("评估进度状态")
    print("=" * 70)

    if not progress_file.exists():
        print("❌ 未找到进度文件, 可能尚未开始评估")
        return 1

    progress = json.loads(progress_file.read_text(encoding="utf-8"))
    print(f"最后处理: 第 {progress['last_index']} 条")
    print(f"总数: {progress['total']}")
    print(f"更新时间: {progress.get('updated_at', 'N/A')}")

    if progress["total"] > 0:
        pct = progress["last_index"] / progress["total"] * 100
        print(f"进度: {pct:.1f}%")

    if results_file.exists():
        results = json.loads(results_file.read_text(encoding="utf-8"))
        print(f"成功记录: {len(results)}")

    if failed_file.exists():
        failed = json.loads(failed_file.read_text(encoding="utf-8"))
        print(f"失败记录: {len(failed)}")

    print("=" * 70)
    return 0


def cmd_clean(args):
    """清理进度文件命令"""
    output_dir = Path(args.output_dir) if args.output_dir else Path(__file__).parent
    progress_file = output_dir / "progress.json"
    results_file = output_dir / "eval_results.json"
    failed_file = output_dir / "failed_records.json"

    for f in [progress_file, results_file, failed_file]:
        if f.exists():
            f.unlink()
            print(f"已删除: {f}")

    print("\n⚠️  数据库数据需手动清理")
    print("=" * 70)
    return 0


def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="意图识别评估脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python intent_eval.py run --input data.xlsx
  python intent_eval.py run --input data.xlsx --output-dir ./output
  python intent_eval.py export --input data.xlsx --output result.xlsx
  python intent_eval.py status --output-dir ./output
  python intent_eval.py clean --output-dir ./output
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    run_parser = subparsers.add_parser("run", help="运行意图识别评估")
    run_parser.add_argument("--input", "-i", required=True, help="输入Excel文件路径")
    run_parser.add_argument("--api", "-a", help=f"API地址 (默认: {Config.API_BASE_URL})")
    run_parser.add_argument("--assistant", help=f"助手类型 (默认: {Config.ASSISTANT_TYPE})")
    run_parser.add_argument("--output-dir", "-o", help="输出目录 (默认: 脚本所在目录)")

    export_parser = subparsers.add_parser("export", help="导出AI回复到Excel")
    export_parser.add_argument("--input", "-i", required=True, help="原始Excel文件路径")
    export_parser.add_argument("--output", "-o", help="输出Excel文件路径")
    export_parser.add_argument("--results", "-r", help="评估结果JSON文件路径")
    export_parser.add_argument("--api", "-a", help=f"API地址 (默认: {Config.API_BASE_URL})")

    status_parser = subparsers.add_parser("status", help="查看评估进度状态")
    status_parser.add_argument("--output-dir", "-o", help="输出目录")

    clean_parser = subparsers.add_parser("clean", help="清理进度文件")
    clean_parser.add_argument("--output-dir", "-o", help="输出目录")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "run":
        return cmd_run(args)
    elif args.command == "export":
        return cmd_export(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "clean":
        return cmd_clean(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
