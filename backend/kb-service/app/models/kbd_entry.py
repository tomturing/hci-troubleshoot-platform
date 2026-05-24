"""
KB Service SQLAlchemy 模型 — kbd_entry

KBD 知识条目表，用于存储深信服案例原始数据。
生命周期：draft → published → archived / rejected

字段设计（双轨制）：
  - 叙述字段（8 大章节）：由 pipeline 从案例 HTML 自动提取，admin 可编辑
    章节字段中图片位置以 ![img:N] 占位符标记，视觉描述存储在 images_json
  - steps_text：自然语言步骤（人类阅读）
  - steps_json：结构化工具步骤（agent 执行，默认为空，需人工/AI 填充）
  - images_json：图片视觉描述列表（pipeline Vision LLM 生成，独立存储）
    格式：[{"seq": N, "section": "field_name", "desc": "..."}]
  - content_md：由章节字段 + images_json 聚合渲染（rebuild_content_md() 生成）
    供 LLM 上下文注入；embedding 不使用此字段

检索索引设计：
  - embedding：embed(title + problem_description + alert_info + root_cause)
    问题侧语义向量，不含 solution 等答案侧字段
  - tsv：to_tsvector(title + content_md)，BM25 全文关键字广召回
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import BigInteger, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class KbdEntry(Base):
    """KBD 知识条目模型

    状态机：draft → published → archived / rejected

    字段说明：
    - support_id: 深信服案例ID（幂等键，唯一）
    - 8 大章节字段: pipeline 自动提取，admin 可单独编辑
      章节字段中图片位置以 ![img:N] 占位符标记
    - images_json: 图片视觉描述（[{"seq": N, "section": field, "desc": "..."}]）
      独立存储，rebuild_content_md() 展开占位符；admin 编辑章节后视觉信息不丢失
    - steps_json: 结构化工具步骤（供 agent 执行，非空时对 InvestigationAgent 可见）
    - content_md: 聚合渲染 Markdown（供展示和 LLM 上下文注入）
    - embedding: embed(title + problem_description + alert_info + root_cause)
      问题侧语义向量，不含 solution，避免答案侧污染向量空间
    - tsv: BM25 全文检索（title + content_md，负责关键字广召回）
    """

    __tablename__ = "kbd_entry"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    support_id = Column(String(20), unique=True, nullable=False)         # 深信服案例ID（幂等键）
    support_url = Column(Text, nullable=True)                            # 原始案例 URL
    title = Column(Text, nullable=False)                                 # 案例标题

    # ── 8 大标准章节（结构化存储）────────────────────────────────────────────
    # 叙述字段：由 pipeline 从案例 HTML 自动提取（Markdown 格式），admin 可编辑
    problem_description = Column(Text, nullable=False, default="")      # 问题描述（必填）
    alert_info = Column(Text, nullable=False, default="")               # 告警信息（可选）
    steps_text = Column(Text, nullable=False, default="")               # 有效排查步骤（自然语言，供人阅读）
    root_cause = Column(Text, nullable=False, default="")               # 根因（必填）
    solution = Column(Text, nullable=False, default="")                 # 解决方案（必填）
    operational_impact = Column(Text, nullable=False, default="")       # 操作影响范围（可选）
    is_temporary = Column(Text, nullable=False, default="")             # 是否是临时解决方案（可选）
    recommendations = Column(Text, nullable=False, default="")          # 建议与总结（可选）

    # ── 结构化工具步骤（供 agent 执行）────────────────────────────────────────
    # 格式：[{"tool_name": "...", "tool_args_template": {...}, "expected_pattern": "..."}]
    # 默认为空，需 admin 人工编辑或 AI 提取后填充
    # 非空时 KBD 条目对 InvestigationAgent 可见（差异诊断）
    steps_json = Column(JSONB, nullable=False, default=list)            # 结构化工具步骤

    # ── 图片视觉描述（pipeline Vision LLM 生成，独立存储）───────────────────
    # 格式：[{"seq": 0, "section": "steps_text", "desc": "TYPE: 日志截图\n..."}]
    # 章节字段中对应位置以 ![img:N] 占位符标记
    # rebuild_content_md() 读取此字段将占位符展开为 > **【截图说明】** 块
    # admin 编辑章节字段后，视觉描述通过此字段保留，不会丢失
    images_json = Column(JSONB, nullable=False, default=list)           # 图片视觉描述列表

    # ── 聚合渲染（含截图视觉描述，供展示和 LLM 上下文注入）──────────────────
    # 由 pipeline 生成（含 > **【截图说明】** 等视觉信息）
    # admin 编辑章节后由 rebuild_content_md() 重建（从章节字段+images_json 生成）
    # 注意：embedding 不使用此字段，embedding 使用问题侧字段（见 SECTION_FIELDS_FOR_EMBEDDING）
    content_md = Column(Text, nullable=True)                            # 聚合渲染 Markdown

    # 使用 entry_metadata 作为 Python 属性名，"metadata" 作为数据库列名
    # 避免 SQLAlchemy Base.metadata 保留属性冲突
    entry_metadata = Column("metadata", JSONB, nullable=False, default=dict)  # 补充元数据

    # ── 分类字段（双轨制）────────────────────────────────────────────────────
    category_id = Column(String(32), nullable=True)                      # 人工确认分类
    ai_category_id = Column(String(32), nullable=True)                   # AI 分类建议
    ai_category_conf = Column(Float, nullable=True)                      # 分类置信度
    ai_category_reason = Column(Text, nullable=True)                     # 分类理由

    # ── 检索字段（published 时生成）──────────────────────────────────────────
    # embedding 字段使用 pgvector，需要在数据库层面定义
    # tsv 字段使用 tsvector，需要在数据库层面定义

    # ── 状态机字段 ────────────────────────────────────────────────────────────
    status = Column(String(20), nullable=False, default="draft")         # draft/published/archived/rejected
    reviewer_id = Column(Integer, nullable=True)                         # 审核人 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)         # 审核时间
    review_note = Column(Text, nullable=True)                            # 审核备注
    published_at = Column(DateTime(timezone=True), nullable=True)        # 发布时间
    archived_at = Column(DateTime(timezone=True), nullable=True)         # 归档时间

    # ── 命中统计（case 级去重，物化列）────────────────────────────────────────
    hit_count = Column(Integer, nullable=False, default=0)               # 有多少个唯一 case 命中此条目（S4 根因确认时 +1）

    # ── 时间戳 ───────────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # 合法状态集合
    VALID_STATUSES = frozenset({"draft", "published", "archived", "rejected"})

    # 8 大章节字段名列表（与 clean_prompt_hci.md 标准章节对应）
    SECTION_FIELDS = (
        "problem_description",
        "alert_info",
        "steps_text",
        "root_cause",
        "solution",
        "operational_impact",
        "is_temporary",
        "recommendations",
    )

    # embedding 使用的问题侧字段（不含 solution 等答案侧字段，避免污染向量空间）
    # embed(title + problem_description + alert_info + root_cause)
    EMBEDDING_FIELDS = ("title", "problem_description", "alert_info", "root_cause")

    def build_embedding_text(self) -> str:
        """构建 embedding 输入文本（问题侧字段拼接）。

        仅使用问题描述侧字段，排除 solution/recommendations 等答案侧字段。
        确保向量空间反映"这是一个关于什么问题的案例"，而非"这个案例如何解决"。
        """
        parts = []
        for field in self.EMBEDDING_FIELDS:
            text = (getattr(self, field, "") or "").strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def rebuild_content_md(self) -> str:
        """从章节字段 + images_json 重建 content_md（admin 编辑后调用）。

        处理逻辑：
        1. 遍历 8 大章节字段，将 ![img:N] 占位符替换为 images_json 中对应的
           > **【截图说明】** 视觉描述块
        2. 拼接为完整的 Markdown 文档

        相比旧版 rebuild_content_md（只含文字），此版本保留了视觉描述，
        因为视觉描述已结构化存储在 images_json 中，即使 admin 编辑章节也不会丢失。
        """
        # 构建 seq → desc 快速查找表
        img_desc: dict[int, str] = {}
        for item in (self.images_json or []):
            seq = item.get("seq")
            desc = item.get("desc", "")
            if seq is not None and desc:
                img_desc[int(seq)] = desc

        section_map = {
            "problem_description": "问题描述",
            "alert_info": "告警信息",
            "steps_text": "有效排查步骤",
            "root_cause": "根因",
            "solution": "解决方案",
            "operational_impact": "操作影响范围",
            "is_temporary": "是否是临时解决方案",
            "recommendations": "建议与总结",
        }

        def _expand_placeholders(text: str) -> str:
            """将 ![img:N] 占位符替换为视觉描述引用块"""
            def _replace(m: re.Match) -> str:
                seq = int(m.group(1))
                desc = img_desc.get(seq, "")
                if not desc:
                    return ""  # 无对应描述则移除占位符
                lines = desc.strip().split("\n")
                if lines[0].startswith("BACKGROUND:") or lines[0].startswith("TYPE:"):
                    # v2 格式：每行独立 ">" 行
                    block_lines = ["> **【截图说明】**"]
                    for line in lines:
                        block_lines.append(f"> {line}" if line.strip() else ">")
                    return "\n\n" + "\n".join(block_lines) + "\n\n"
                else:
                    # v1 格式
                    return f"\n\n> **【截图说明】**：{desc.strip()}\n\n"

            return re.sub(r'!\[img:(\d+)\]', _replace, text)

        parts = []
        for field, heading in section_map.items():
            text = (getattr(self, field, "") or "").strip()
            if text:
                expanded = _expand_placeholders(text).strip()
                if expanded:
                    parts.append(f"## {heading}\n\n{expanded}")
        return "\n\n".join(parts)

    def __repr__(self) -> str:
        return f"<KbdEntry(id={self.id}, support_id={self.support_id}, status={self.status})>"
