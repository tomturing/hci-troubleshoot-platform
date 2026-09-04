#!/usr/bin/env python3
"""
scripts/seed_signal_best_practices.py
从已发布的 23 个 KBD 中清洗提取专家黄金信号，初始化注入到 signal_best_practice 表。
支持 --dry-run 查看待入库资产。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

# 注入 backend 路径以便复用 SQLAlchemy 模型与 DB 连接
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
KB_SERVICE_DIR = os.path.join(PROJECT_ROOT, "backend", "kb-service")
SHARED_DIR = os.path.join(PROJECT_ROOT, "backend", "shared")

sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
sys.path.insert(0, KB_SERVICE_DIR)


from app.models.signal_assets import SignalBestPractice, SignalModelingTemplate
from shared.database.postgres import DatabaseManager
from sqlalchemy import select, text
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_signal_best_practices")


def _infer_pattern_category(tool: str, signal: dict) -> str:
    """根据信号结构和参数推断场景模式分类"""
    acquire = signal.get("acquire") or {}
    args = acquire.get("args") or {}
    match = signal.get("match") or {}
    match_type = match.get("type") if isinstance(match, dict) else "none"

    if tool == "qkv_task":
        kw = args.get("keyword", "")
        return f"任务失败捕获与变量提取({kw[:15]})"
    elif tool == "qkv_alert":
        return "平台异步告警查询"
    elif tool == "qkv_dialog":
        return "前端弹框复合取值"
    elif tool == "qfk_log":
        file_name = args.get("file", "unknown")
        if match_type == "threshold":
            return f"日志数值提取与阈值计算({file_name})"
        elif match_type == "regex":
            return f"日志正则模式匹配({file_name})"
        else:
            return f"日志关键错误行判定({file_name})"
    elif tool == "qfk_system":
        cmd = args.get("command", "")
        if match_type == "threshold":
            return f"系统命令输出阈值判定({cmd})"
        return f"系统命令状态检查({cmd})"
    elif tool == "qfk_vm":
        return "虚拟机只读配置与状态检查"
    elif tool == "qfk_storage":
        return "存储卷与磁盘状态探针"
    return f"{tool}标准排查探针"


async def main():
    parser = argparse.ArgumentParser(description="提取已发布 KBD 黄金信号到 signal_best_practice")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不实际落库")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # 默认尝试从 k8s / 本地环境变量推断
        user = os.environ.get("POSTGRES_USER", "hci_admin")
        pwd = os.environ.get("POSTGRES_PASSWORD", "hci_admin_password")
        host = os.environ.get("POSTGRES_HOST", "postgres")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ.get("POSTGRES_DB", "hci_troubleshoot")
        db_url = f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"

    logger.info("初始化数据库连接...")
    db_manager = DatabaseManager(db_url)
    await db_manager.initialize()

    async with db_manager.async_session_factory() as session:
        # 1. 加载 template 字典
        template_res = await session.execute(select(SignalModelingTemplate))
        templates = {t.tool_name: t.id for t in template_res.scalars().all()}
        logger.info("已加载 %d 个信号模板定义: %s", len(templates), list(templates.keys()))

        # 2. 查询已发布的 23 个 KBD
        kbd_res = await session.execute(
            text("""
            SELECT id, support_id, title, problem_description, alert_info, steps_text, signals_json
            FROM kbd_entry
            WHERE status = 'published'
            ORDER BY id ASC;
            """)
        )
        kbds = kbd_res.fetchall()
        logger.info("获取到 %d 个已发布 KBD 条目", len(kbds))

        instances_to_create = []
        tool_counts = {}

        for kbd in kbds:
            kbd_id = kbd.id
            support_id = kbd.support_id
            signals_doc = kbd.signals_json or {}
            signals = signals_doc.get("signals") if isinstance(signals_doc, dict) else (signals_doc if isinstance(signals_doc, list) else [])
            if not signals:
                continue

            for idx, sig in enumerate(signals):
                if not isinstance(sig, dict):
                    continue
                acquire = sig.get("acquire") or {}
                tool = acquire.get("tool")
                if not tool:
                    continue

                template_id = templates.get(tool)
                pattern_cat = _infer_pattern_category(tool, sig)
                evidence = (sig.get("provenance") or {}).get("evidence") or (acquire.get("args") or {}).get("instruction") or kbd.title
                notes = (sig.get("review") or {}).get("notes") or f"来源于已发布 KBD {support_id} 专家最终审查版本"

                instances_to_create.append({
                    "template_id": template_id,
                    "tool_name": tool,
                    "pattern_category": pattern_cat,
                    "source_kbd_id": kbd_id,
                    "support_id": support_id,
                    "raw_evidence": str(evidence),
                    "signal_json": sig,
                    "design_notes": notes,
                    "completeness_score": 10
                    ,"trace_id": f"seed-best-practice:{uuid4().hex}"
                })
                tool_counts[tool] = tool_counts.get(tool, 0) + 1

        logger.info("共清洗整理出 %d 个黄金最佳实践信号。按工具分布: %s", len(instances_to_create), tool_counts)

        if args.dry_run:
            logger.info("【DRY-RUN】演练模式，前 3 条样本展示:")
            for item in instances_to_create[:3]:
                print(f"\n--- [{item['tool_name']}] KBD {item['support_id']} | {item['pattern_category']} ---")
                print(f"Evidence: {item['raw_evidence']}")
                print(f"Signal ID: {item['signal_json'].get('id')} | Match: {bool(item['signal_json'].get('match'))}")
            print(f"\nTotal extracted: {len(instances_to_create)}")
            return

        # 3. 实际写入数据库
        # 先清空旧的种子数据（如果存在）
        await session.execute(text("DELETE FROM signal_best_practice WHERE source_kbd_id IS NOT NULL;"))
        for inst in instances_to_create:
            bp = SignalBestPractice(**inst)
            session.add(bp)

        await session.commit()
        logger.info("成功将 %d 个黄金最佳实践信号入库到 signal_best_practice 表！", len(instances_to_create))


if __name__ == "__main__":
    asyncio.run(main())
