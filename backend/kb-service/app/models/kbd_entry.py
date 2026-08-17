"""
KB Service SQLAlchemy 模型 — kbd_entry

KBD 知识条目表，用于存储深信服案例原始数据。
生命周期：draft → published → archived / rejected

字段设计（双轨制）：
  - 叙述字段（8 大章节）：由 pipeline 从案例 HTML 自动提取，admin 可编辑
    章节字段中图片位置以 ![img:N] 占位符标记，视觉描述存储在 images_json
  - steps_text：自然语言步骤（人类阅读）
  - signals_json：关键信号集合（producer/consumer，agent 执行与判定，默认[]，抽取阶段填充）
  - images_json：图片视觉描述列表（pipeline Vision LLM 生成，独立存储）
    格式：[{"seq": N, "section": "field_name", "desc": "..."}]
  - content_md：由章节字段 + images_json 聚合渲染（rebuild_content_md() 生成）
    供 LLM 上下文注入；embedding 不使用此字段

检索索引设计：
  - embedding：embed(title + problem_description + alert_info + root_cause)
    问题侧语义向量，不含 solution 等答案侧字段
  - tsv：jieba 分词后写入 PostgreSQL tsvector，负责中文词法召回
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from shared.database.postgres import Base
from sqlalchemy import BigInteger, Column, DateTime, Float, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB


def _agent_safe_image_desc(item: dict[str, Any]) -> str:
    """生成供 Agent/content_md 使用的截图文本，隔离未验证语义推断。

    ``images_json`` 继续保留完整 DESCRIPTION 供管理端审核；content_md 只展开可观察
    文字。旧数据没有 Evidence 质量契约，也按未验证处理，避免历史自由文本被当成事实。
    """

    desc = str(item.get("desc") or "").strip()
    description_marker = "\nDESCRIPTION:\n"
    if not desc or description_marker not in desc:
        return desc
    evidence = item.get("evidence")
    quality = evidence.get("quality") if isinstance(evidence, dict) else {}
    quality = quality if isinstance(quality, dict) else {}
    inference_status = str(quality.get("inference_status") or "legacy_unverified")
    inference_needs_review = bool(quality.get("inference_needs_review")) or inference_status in {
        "unverified",
        "needs_review",
        "legacy_unverified",
    }
    if not inference_needs_review:
        return desc

    observed_part = desc.split(description_marker, 1)[0].rstrip()
    return (
        f"{observed_part}\n"
        f"INFERENCE_STATUS: {inference_status}\n"
        "INFERENCE_NOTICE: 模型语义描述未进入 Agent 文档；请仅使用 FULL_TEXT/Observed Facts。"
    )


def _image_desc_variants(item: dict[str, Any]) -> list[str]:
    """返回可能出现在 content_md 中的新旧描述，供反向同步兼容。"""

    raw = str(item.get("desc") or "").strip()
    safe = _agent_safe_image_desc(item)
    return list(dict.fromkeys(desc for desc in (raw, safe) if desc))


def strip_markdown(text: str) -> str:
    """去除 Markdown 语法标记和图片占位符，返回干净纯净的纯文本内容。

    注意：保留代码块和行内代码的内容，仅移除语法标记。
    保留标识符中的下划线和星号（如 os_type, file_name）。
    """
    if not text:
        return ""
    # 1. 移除图片占位符和图片标记 (e.g. ![img:0], ![无描述])
    text = re.sub(r"!\[img:\d+\]", "", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"> \*\*【截图说明】\*\*.*", "", text)  # 移除截图说明提示行
    # 2. 移除普通链接格式 [text](url)，仅保留文本部分
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    # 3. 移除 HTML 标签
    text = re.sub(r"<[^>]*>", "", text)
    # 4. 移除粗体/斜体语法界定符（仅匹配成对的 ** __ * _，保留标识符中的字符）
    #    先处理多字符界定符（避免部分匹配问题）
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **text** → text
    text = re.sub(r"__(.+?)__", r"\1", text)  # __text__ → text
    text = re.sub(r"\*(.+?)\*", r"\1", text)  # *text* → text（单星号斜体）
    text = re.sub(r"_(.+?)_", r"\1", text)  # _text_ → text（单下划线斜体）
    # 5. 移除标题标志 (e.g. ## 问题描述)
    text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)
    # 6. 移除引用块前导符 (> )
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    # 7. 移除代码块 fence 标记，保留代码内容（```...``` → 内部代码）
    text = re.sub(r"^```.*$", "", text, flags=re.MULTILINE)  # 移除 ``` 行
    text = re.sub(r"`([^`]+)`", r"\1", text)  # `code` → code（保留内容）
    # 8. 移除无序列表和有序列表前导符 (-, *, +, \d+.)
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # 9. 规范化并清洗多余空白字符与换行
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def build_kbd_embedding_text(
    *,
    title: str | None,
    problem_description: str | None,
    alert_info: str | None,
    root_cause: str | None,
    fallback_text: str | None = None,
) -> str:
    """从问题侧字段构建稳定的 embedding 输入。"""
    parts = [title, problem_description, alert_info, root_cause]
    text = "\n\n".join(part.strip() for part in parts if part and part.strip())
    return strip_markdown(text or fallback_text or "")


class KbdEntry(Base):
    """KBD 知识条目模型

    状态机：draft → published → archived / rejected

    字段说明：
    - support_id: 深信服案例ID（幂等键，唯一）
    - 8 大章节字段: pipeline 自动提取，admin 可单独编辑
      章节字段中图片位置以 ![img:N] 占位符标记
    - images_json: 图片视觉描述（[{"seq": N, "section": field, "desc": "..."}]）
      独立存储，rebuild_content_md() 展开占位符；admin 编辑章节后视觉信息不丢失
    - signals_json: 关键信号集合（producer/consumer 信号，非空时对 InvestigationAgent 可见）
    - content_md: 聚合渲染 Markdown（供展示和 LLM 上下文注入）
    - embedding: embed(title + problem_description + alert_info + root_cause)
      问题侧语义向量，不含 solution，避免答案侧污染向量空间
    - tsv: jieba + PostgreSQL FTS（title + content_md，负责中文关键字召回）
    """

    __tablename__ = "kbd_entry"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    support_id = Column(String(20), unique=True, nullable=False)  # 深信服案例ID（幂等键）

    title = Column(Text, nullable=False)  # 案例标题

    # ── 8 大标准章节（结构化存储）────────────────────────────────────────────
    # 叙述字段：由 pipeline 从案例 HTML 自动提取（Markdown 格式），admin 可编辑
    problem_description = Column(Text, nullable=False, default="")  # 问题描述（必填）
    alert_info = Column(Text, nullable=False, default="")  # 告警信息（可选）
    steps_text = Column(Text, nullable=False, default="")  # 有效排查步骤（自然语言，供人阅读）
    root_cause = Column(Text, nullable=False, default="")  # 根因（必填）
    solution = Column(Text, nullable=False, default="")  # 解决方案（必填）
    operational_impact = Column(Text, nullable=False, default="")  # 操作影响范围（可选）
    is_temporary = Column(Text, nullable=False, default="")  # 是否是临时解决方案（可选）
    recommendations = Column(Text, nullable=False, default="")  # 建议与总结（可选）

    # ── 关键信号集合（供 agent 执行与判定）────────────────────────────────────
    # v2 嵌套文档格式：[{"acquire":{"tool","args"}, "match":{...}, "orchestrate":{"produces","requires"}, "provenance":{"category"}}]
    # provenance.category: frontend(producer=QKV) / backend(consumer=QFK)
    # acquire.args / match 内占位符统一 {{VAR}} 大写（见 ADR-2）
    # 默认为空，由"关键信号分级抽取"阶段（pipeline Stage.EXTRACT_SIGNALS / 审核期）填充
    # 非空时 KBD 条目对 InvestigationAgent 可见（差异诊断）
    signals_json = Column(JSONB, nullable=False, default=list)  # 关键信号集合

    # ── 图片视觉描述（pipeline Vision LLM 生成，独立存储）───────────────────
    # 格式：[{"seq": 0, "section": "steps_text", "desc": "TYPE: 日志截图\n..."}]
    # 章节字段中对应位置以 ![img:N] 占位符标记
    # rebuild_content_md() 读取此字段将占位符展开为 > **【截图说明】** 块
    # admin 编辑章节字段后，视觉描述通过此字段保留，不会丢失
    images_json = Column(JSONB, nullable=False, default=list)  # 图片视觉描述列表

    # ── 聚合渲染（含截图视觉描述，供展示和 LLM 上下文注入）──────────────────
    # 由 pipeline 生成（含 > **【截图说明】** 等视觉信息）
    # admin 编辑章节后由 rebuild_content_md() 重建（从章节字段+images_json 生成）
    # 注意：embedding 不使用此字段，embedding 使用问题侧字段（见 SECTION_FIELDS_FOR_EMBEDDING）
    content_md = Column(Text, nullable=True)  # 聚合渲染 Markdown
    content_raw = Column(Text, nullable=True)  # 纯文本去噪内容

    # 使用 entry_metadata 作为 Python 属性名，"metadata" 作为数据库列名
    # 避免 SQLAlchemy Base.metadata 保留属性冲突
    entry_metadata = Column("metadata", JSONB, nullable=False, default=dict)  # 补充元数据

    # ── 分类字段（双轨制）────────────────────────────────────────────────────
    category_id = Column(String(32), nullable=True)  # 人工确认分类
    ai_category_id = Column(String(32), nullable=True)  # AI 分类建议
    ai_category_conf = Column(Float, nullable=True)  # 分类置信度
    ai_category_reason = Column(Text, nullable=True)  # 分类理由

    # ── 检索字段（published 时生成）──────────────────────────────────────────
    # embedding 字段使用 pgvector，需要在数据库层面定义
    # tsv 字段使用 tsvector，需要在数据库层面定义
    embedding_model = Column(String(100), nullable=True)  # 向量模型；为空的历史向量不得参与检索
    embedding_content_hash = Column(String(64), nullable=True)  # 生成向量时输入文本的 SHA-256
    embedding_updated_at = Column(DateTime(timezone=True), nullable=True)  # 向量最后成功生成时间

    # ── 状态机字段 ────────────────────────────────────────────────────────────
    status = Column(String(20), nullable=False, default="draft")  # draft/published/archived/rejected
    reviewer_id = Column(Integer, nullable=True)  # 审核人 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)  # 审核时间
    review_note = Column(Text, nullable=True)  # 审核备注
    published_at = Column(DateTime(timezone=True), nullable=True)  # 发布时间

    # ── 命中统计（case 级去重，物化列）────────────────────────────────────────
    hit_count = Column(Integer, nullable=False, default=0)  # 有多少个唯一 case 命中此条目（S4 根因确认时 +1）

    # ── 轻治理版本 head ───────────────────────────────────────────────────────
    # 历史 payload 存入 append-only kbd_revision；本表继续是 Admin/检索兼容主记录。
    latest_proposal_revision_id = Column(BigInteger, nullable=True)
    working_revision_id = Column(BigInteger, nullable=True)
    lock_version = Column(Integer, nullable=False, default=1)  # P1-1 修复：从 1 开始，与数据库 DEFAULT 1 对齐

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
        return build_kbd_embedding_text(
            title=self.title,
            problem_description=self.problem_description,
            alert_info=self.alert_info,
            root_cause=self.root_cause,
            fallback_text=self.content_raw or self.content_md,
        )

    def rebuild_content_md(self, old_images_json: list[dict[str, Any]] | None = None) -> str:
        """从章节字段 + images_json 重建 content_md（admin 编辑后调用）。

        处理逻辑：
        1. 检查 8 大章节字段是否全部为空。
        2. 如果全部为空：说明是旧版/单文档导入的非结构化案例，没有 8 大章节。
           直接以当前的 self.content_md 为基础模板。
           如果提供了 old_images_json，其将旧描述块在 content_md 中安全地替换回 ![img:seq] 占位符。
           然后对还原了占位符的 content_md，用最新 images_json 中的视觉描述展开它。
        3. 如果不全为空：按照常规流程，遍历 8 大章节，将 ![img:N] 占位符替换为 images_json 中对应的
           > **【截图说明】** 视觉描述块，并拼接为完整的 Markdown 文档
        """
        # 构建 seq → Agent 安全 desc 快速查找表；完整推断仍保留在 images_json。
        img_desc: dict[int, str] = {}
        for item in self.images_json or []:
            seq = item.get("seq")
            desc = _agent_safe_image_desc(item)
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
                    # Vision 尚未识别（desc 为空）时，保留一个最简占位块而非删除。
                    # 若直接返回 "" 会把 ![img:N] 从章节字段和 content_md 中永久抹除，
                    # 导致后续 Vision 成功后 rebuild_content_md 也无法找回图片位置。
                    return f"\n\n> **【截图 {seq}】**（待识图）\n\n"
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

            return re.sub(r"!\[img:(\d+)\]", _replace, text)

        # 检查 8 大章节字段是否全部为空
        has_any_section = any((getattr(self, field, "") or "").strip() for field in section_map)

        if not has_any_section:
            # 如果 8 大章节全空，直接使用当前的 content_md 作为模版进行局部替换
            content_md = self.content_md or ""
            target_images_json = old_images_json if old_images_json is not None else (self.images_json or [])

            for item in target_images_json:
                seq = item.get("seq")
                if seq is not None:
                    for desc in _image_desc_variants(item):
                    # 构造 v2 格式引用块文本
                        lines = desc.strip().split("\n")
                        block_lines = ["> **【截图说明】**"]
                        for line in lines:
                            block_lines.append(f"> {line}" if line.strip() else ">")
                        v2_block = "\n".join(block_lines)

                        # 构造 v1 格式引用块文本
                        v1_block = f"> **【截图说明】**：{desc.strip()}"

                        # 同时兼容历史原始 DESCRIPTION 和新版 Agent 安全块。
                        pattern_v2 = re.compile(r"\n*\s*" + re.escape(v2_block) + r"\s*\n*")
                        content_md, count = pattern_v2.subn(f"\n\n![img:{seq}]\n\n", content_md)
                        if count == 0:
                            pattern_v1 = re.compile(r"\n*\s*" + re.escape(v1_block) + r"\s*\n*")
                            content_md = pattern_v1.sub(f"\n\n![img:{seq}]\n\n", content_md)

            # 使用最新的图片说明展开所有的占位符
            return _expand_placeholders(content_md).strip()

        # 正常章节拼接流程
        parts = []
        for field, heading in section_map.items():
            text = (getattr(self, field, "") or "").strip()
            if text:
                expanded = _expand_placeholders(text).strip()
                if expanded:
                    parts.append(f"## {heading}\n\n{expanded}")
        return "\n\n".join(parts)

    def sync_sections_from_content_md(self) -> None:
        """从当前的 content_md 反向解析并填充 8 大章节字段，确保双轨制数据完全同步。"""
        content_md = self.content_md or ""

        # 1. 结合 images_json，将 content_md 中的图片说明块还原回 ![img:seq] 占位符
        # 1a. 还原"待识图"占位块（Vision 识别失败时由 _expand_placeholders 生成）
        #     格式：> **【截图 N】**（待识图）
        content_md = re.sub(
            r"\n*\s*>\s*\*\*【截图\s*(\d+)】\*\*（待识图）\s*\n*",
            lambda m: f"\n\n![img:{m.group(1)}]\n\n",
            content_md,
        )

        # 1b. 结合 images_json，将已识别的图片说明块还原回 ![img:seq] 占位符
        for item in self.images_json or []:
            seq = item.get("seq")
            if seq is not None:
                for desc in _image_desc_variants(item):
                # 构造 v2 格式引用块文本
                    lines = desc.strip().split("\n")
                    block_lines = ["> **【截图说明】**"]
                    for line in lines:
                        block_lines.append(f"> {line}" if line.strip() else ">")
                    v2_block = "\n".join(block_lines)

                    # 构造 v1 格式引用块文本
                    v1_block = f"> **【截图说明】**：{desc.strip()}"

                    # 在 content_md 中正则替换掉这两种可能的旧引用块，还原回占位符
                    pattern_v2 = re.compile(r"\n*\s*" + re.escape(v2_block) + r"\s*\n*")
                    content_md, count = pattern_v2.subn(f"\n\n![img:{seq}]\n\n", content_md)
                    if count == 0:
                        pattern_v1 = re.compile(r"\n*\s*" + re.escape(v1_block) + r"\s*\n*")
                        content_md = pattern_v1.sub(f"\n\n![img:{seq}]\n\n", content_md)

        # 2. 按 ## 标题 切分文本并填充到各个字段
        section_map = {
            "问题描述": "problem_description",
            "告警信息": "alert_info",
            "有效排查步骤": "steps_text",
            "根因": "root_cause",
            "解决方案": "solution",
            "操作影响范围": "operational_impact",
            "是否是临时解决方案": "is_temporary",
            "建议与总结": "recommendations",
        }

        # 初始化所有章节字段为 ""
        for field in section_map.values():
            setattr(self, field, "")

        pattern = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
        matches = list(pattern.finditer(content_md))

        for i, match in enumerate(matches):
            heading = match.group(1).strip()
            field = section_map.get(heading)
            if not field:
                continue
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content_md)
            text_val = content_md[start:end].strip()
            setattr(self, field, text_val)

    def ensure_images_json_complete(self) -> bool:
        """确保 images_json 包含 content_md 中引用的所有图片。

        解析 content_md 中的 ![img:N] 占位符，检查 images_json 是否有对应条目。
        对于缺失的 seq，创建占位条目（desc 为空字符串）。

        Returns:
            bool: 是否有新增条目（用于日志记录）
        """
        content_md = self.content_md or ""

        # 1. 提取 content_md 中所有 ![img:N] 占位符的 seq
        img_placeholders = re.findall(r"!\[img:(\d+)\]", content_md)
        referenced_seqs = {int(seq) for seq in img_placeholders}

        if not referenced_seqs:
            return False

        # 2. 获取 images_json 中已有的 seq
        existing_seqs = {item.get("seq") for item in (self.images_json or []) if item.get("seq") is not None}

        # 3. 找出缺失的 seq
        missing_seqs = referenced_seqs - existing_seqs

        if not missing_seqs:
            return False

        # 4. 为缺失的 seq 创建占位条目
        images_json = [dict(item) for item in (self.images_json or [])]
        for seq in sorted(missing_seqs):
            images_json.append({
                "seq": seq,
                "section": "steps_text",  # 默认归属排障步骤
                "context_before": "",
                "context_after": "",
                "desc": "",  # 占位，等待 Vision LLM 填充
            })

        # 5. 按 seq 排序后写回
        images_json.sort(key=lambda x: x["seq"])
        self.images_json = images_json

        return True

    def __repr__(self) -> str:
        return f"<KbdEntry(id={self.id}, support_id={self.support_id}, status={self.status})>"


class KbdImage(Base):
    """KBD 原始图片模型 - 存储 data-pipeline 抓取的原始图片二进制

    用途：解耦 kb-service 对 data-pipeline 本地文件系统的依赖，
    支持 admin-ui 在线触发"重新识图"按钮（POST /api/admin/kbd/{id}/reanalyze-images）

    关系：
    - kbd_entry_id -> kbd_entry.id（ON DELETE CASCADE，跟随 KBD 条目删除）
    - seq 与 kbd_entry.images_json 中的 seq 对应
    """

    __tablename__ = "kbd_image"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kbd_entry_id = Column(BigInteger, nullable=False, comment="关联的 KBD 条目 ID")
    seq = Column(Integer, nullable=False, comment="图片序号（与 images_json 中的 seq 对应）")
    image_data = Column(LargeBinary, nullable=False, comment="原始图片二进制（压缩后存入）")
    mime_type = Column(String(50), nullable=True, comment="图片 MIME 类型")
    width = Column(Integer, nullable=True, comment="图片宽度（像素）")
    height = Column(Integer, nullable=True, comment="图片高度（像素）")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<KbdImage(id={self.id}, kbd_entry_id={self.kbd_entry_id}, seq={self.seq})>"
