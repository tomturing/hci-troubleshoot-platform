"""
意图识别评估脚本（支持断点续传）

功能：
1. 从 Excel 读取问题描述
2. 批量创建工单（标题包含问题编号）
3. 创建对话并发送消息触发意图识别
4. 等待 SSE 流完整结束，确保 AI 响应入库
5. 支持中途中断后继续执行（断点续传）
6. 单条失败不影响整体流程
"""

import asyncio
import json
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

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


class IntentEvaluation:
    """意图识别评估类"""

    def __init__(self, api_base_url: str = "http://172.22.73.249"):
        self.api_base_url = api_base_url
        self.client_id = f"eval-{uuid.uuid4().hex[:8]}"

    async def create_case(self, title: str, description: str) -> dict:
        """创建工单"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.api_base_url}/api/cases/",
                json={
                    "client_id": self.client_id,
                    "title": title,
                    "description": description,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def create_conversation(self, case_id: str, assistant_type: str = "glm-5") -> dict:
        """创建对话"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.api_base_url}/api/conversations/",
                params={
                    "case_id": case_id,
                    "assistant_type": assistant_type,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def send_message_and_wait(self, conversation_id: str, case_id: str, content: str) -> str:
        """发送消息并等待完整 SSE 流"""
        ai_content = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.api_base_url}/api/conversations/{conversation_id}/message",
                json={
                    "case_id": case_id,
                    "role": "user",
                    "content": content,
                },
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if "content" in chunk:
                                ai_content.append(chunk["content"])
                        except json.JSONDecodeError:
                            pass
        return "".join(ai_content)

    async def process_record(
        self, problem_id: str, problem_desc: str, index: int, assistant_type: str = "glm-5"
    ) -> dict:
        """处理单条记录"""
        # 1. 创建工单
        title = f"[{problem_id}] {problem_desc}"
        case = await self.create_case(title=title, description=problem_desc)
        case_id = case["case_id"]

        # 2. 创建对话
        conv = await self.create_conversation(case_id=case_id, assistant_type=assistant_type)
        conversation_id = conv["conversation_id"]

        # 3. 发送消息并等待完整 SSE 流
        ai_response = await self.send_message_and_wait(conversation_id, case_id, problem_desc)

        return {
            "index": index,
            "problem_id": problem_id,
            "problem_desc": problem_desc,
            "case_id": case_id,
            "conversation_id": conversation_id,
            "assistant_type": assistant_type,
            "ai_response_preview": ai_response[:300] if ai_response else "",
            "status": "success",
            "processed_at": datetime.now().isoformat(),
        }


class EvaluationRunner:
    """评估运行器（支持断点续传）"""

    def __init__(
        self,
        excel_path: str,
        api_base_url: str,
        assistant_type: str,
        output_dir: Path,
    ):
        self.excel_path = excel_path
        self.api_base_url = api_base_url
        self.assistant_type = assistant_type
        self.output_dir = output_dir

        # 进度和结果文件
        self.progress_file = output_dir / "progress.json"
        self.results_file = output_dir / "eval_results.json"
        self.failed_file = output_dir / "failed_records.json"

        # 加载已有进度
        self.progress = self._load_progress()
        self.results = self._load_results()
        self.failed = self._load_failed()

        # 统计
        self.stats = {
            "total": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
        }

    def _load_progress(self) -> dict:
        """加载进度文件"""
        if self.progress_file.exists():
            return json.loads(self.progress_file.read_text())
        return {"last_index": 0, "total": 0}

    def _save_progress(self, last_index: int, total: int):
        """保存进度"""
        self.progress_file.write_text(
            json.dumps({"last_index": last_index, "total": total, "updated_at": datetime.now().isoformat()})
        )

    def _load_results(self) -> list[dict]:
        """加载已有结果"""
        if self.results_file.exists():
            return json.loads(self.results_file.read_text())
        return []

    def _save_results(self):
        """保存结果（追加模式）"""
        self.results_file.write_text(json.dumps(self.results, ensure_ascii=False, indent=2))

    def _load_failed(self) -> list[dict]:
        """加载失败记录"""
        if self.failed_file.exists():
            return json.loads(self.failed_file.read_text())
        return []

    def _save_failed(self):
        """保存失败记录"""
        self.failed_file.write_text(json.dumps(self.failed, ensure_ascii=False, indent=2))

    async def run(self) -> dict:
        """运行评估"""
        df = pd.read_excel(self.excel_path)
        total = len(df)
        self.stats["total"] = total

        start_index = self.progress.get("last_index", 0)
        if start_index > 0:
            print(f"\n📦 断点续传：从第 {start_index + 1} 条继续（共 {total} 条）")
        else:
            print(f"\n🚀 开始处理：共 {total} 条记录")

        evaluator = IntentEvaluation(self.api_base_url)

        # 处理每条记录
        for i in range(start_index, total):
            if _interrupted:
                self._save_progress(i, total)
                self._save_results()
                self._save_failed()
                print(f"✅ 进度已保存至第 {i} 条，下次运行将从这里继续")
                break

            row = df.iloc[i]
            problem_id = str(row["问题编号"])
            problem_desc = str(row["问题描述"])

            # 进度显示
            progress_pct = (i + 1) / total * 100
            print(f"[{i+1}/{total}] ({progress_pct:.1f}%) {problem_id}: {problem_desc[:30]}...")

            try:
                result = await evaluator.process_record(
                    problem_id, problem_desc, i + 1, self.assistant_type
                )
                self.results.append(result)
                self.stats["success"] += 1

                # 每处理 10 条保存一次进度
                if (i + 1) % 10 == 0:
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
                # 失败也保存进度，避免重复尝试
                self._save_progress(i + 1, total)
                self._save_failed()

            self.stats["processed"] = i + 1

        # 最终保存
        if not _interrupted:
            self._save_progress(total, total)
            self._save_results()
            self._save_failed()

        return self.stats


async def main():
    """主函数"""
    # 配置
    excel_path = Path(__file__).parent.parent.parent / "HCI-内存硬盘非P4-260101-26058.xlsx"
    api_base_url = "http://172.22.73.249"
    assistant_type = "glm-5"
    output_dir = Path(__file__).parent

    print("=" * 70)
    print("意图识别评估脚本（全量处理 + 断点续传）")
    print("=" * 70)
    print(f"Excel: {excel_path}")
    print(f"API: {api_base_url}")
    print(f"助手类型: {assistant_type}")
    print(f"输出目录: {output_dir}")
    print("=" * 70)
    print("⚠️ 预计耗时: 约 19 小时（2306 条 × 30秒/条）")
    print("💡 按 Ctrl+C 可安全中断，下次运行自动续传")
    print("=" * 70)

    # 检查文件
    if not excel_path.exists():
        print(f"❌ Excel 文件不存在: {excel_path}")
        return

    # 检查 API
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_base_url}/api/cases/all?limit=1", timeout=5.0)
            print(f"✅ API 服务正常")
    except Exception as e:
        print(f"❌ API 不可达: {e}")
        return

    # 运行评估
    runner = EvaluationRunner(str(excel_path), api_base_url, assistant_type, output_dir)
    stats = await runner.run()

    # 输出统计
    print("\n" + "=" * 70)
    print("处理完成统计:")
    print(f"  总数: {stats['total']}")
    print(f"  成功: {stats['success']}")
    print(f"  失败: {stats['failed']}")
    print(f"  失败记录: {output_dir / 'failed_records.json'}")
    print(f"  结果文件: {output_dir / 'eval_results.json'}")
    print("=" * 70)
    print(f"\n请在 admin-ui 查看: http://172.22.73.249/admin/")


if __name__ == "__main__":
    asyncio.run(main())