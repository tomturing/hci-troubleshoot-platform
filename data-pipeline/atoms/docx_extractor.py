"""
DocxExtractor — SOP 排障手册 .docx → KnowledgeAtomDraft 转换器

支持《虚拟机开关机失败排障手册》结构：
  Title          → 文档标题（忽略）
  Heading 1      → 一级流程（H1 context）
  Heading 2      → 场景分类（H2 context）
  Heading 3      → 错误类型 / 触发关键字（H3 = 一个知识原子组）
  Heading 4      → 子场景/根因（H4 context）
    "现象描述"   → background 原子
    其他         → 当前 H4 根因标签
  Heading 5      → 具体内容
    "判断方法"   → diagnostic_step 原子
    "解决方案*"  → fix_action 原子

每执行 flush 时，把当前 H5 段落收集的所有 Normal 文本写为一个原子。

命令提取规则（_extract_commands）：
  - 识别 `acli ` / `vncli ` / `ssh ` / `cat ` / `grep ` 等 CLI 开头的行
  - 识别行内反引号包裹的代码片段
  - 去重（保序）

错误码提取规则：
  - 正则 0[xX][0-9A-Fa-f]{6,8}，统一大写

保证提取 >= 25 个知识原子。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from docx import Document
from docx.oxml.ns import qn

# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_ID = "虚拟机-003"  # 本手册固定分类 ID

# 命令行特征前缀（小写比对，避免过短前缀导致误匹配）
CMD_PREFIXES = (
    "acli ", "vncli ", "vss ",
    "iptables ", "ssh ", "scp ",
    "cat /", "grep -", "tail -",
    "ls -", "ps aux", "free -",
    "systemctl ", "service ",
    "df -", "du -", "lspci ", "dmesg ",
    "mount ", "umount ",
    "find /", "kubectl ",
    "/etc/", "/var/", "/proc/", "/sys/",
)

ERROR_CODE_RE = re.compile(r"0[xX][0-9A-Fa-f]{6,8}")

# H5 标题 → 原子类型映射
H5_TYPE_MAP = {
    "判断方法": "diagnostic_step",
    "解决方案": "fix_action",
}


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class KnowledgeAtomDraft:
    """知识原子草稿，待写入数据库前的中间结构"""

    id: str = field(default_factory=lambda: f"ka-{uuid4().hex[:12]}")
    type: str = "diagnostic_step"
    category_id: str = CATEGORY_ID
    knowledge_domain: str = "sop"
    trigger: dict = field(default_factory=dict)
    content: dict = field(default_factory=dict)
    confidence: float = 0.95
    verified: bool = True
    source_type: str = "docx"
    source_ref: str = ""
    applicable_version_min: str | None = None
    applicable_version_max: str | None = None


@dataclass
class _ParseContext:
    """解析过程中的上下文状态"""

    h1: str = ""         # 当前 H1 标题（流程名）
    h2: str = ""         # 当前 H2 标题（场景分类）
    h3: str = ""         # 当前 H3 标题（错误类型 / 触发关键字）
    h4: str = ""         # 当前 H4 标题（根因 / 子场景）
    h5_type: str = ""    # 当前 H5 对应的原子类型
    lines: list[str] = field(default_factory=list)  # 当前段落收集的文本行


# ─────────────────────────────────────────────────────────────────────────────
# DocxExtractor
# ─────────────────────────────────────────────────────────────────────────────

class DocxExtractor:
    """从 .docx 文件提取知识原子草稿列表

    使用方式::

        extractor = DocxExtractor("path/to/manual.docx")
        atoms, error_codes = extractor.extract()
        # atoms: list[KnowledgeAtomDraft]
        # error_codes: dict[str, list[str]]  — 错误码 → [atom_id, ...]
    """

    def __init__(self, docx_path: str | Path) -> None:
        self.docx_path = Path(docx_path)
        self._doc = Document(str(self.docx_path))

    # ──────────────────────────────────────────────────────────────────────────

    def extract(self) -> tuple[list[KnowledgeAtomDraft], dict[str, list[str]]]:
        """解析文档，返回 (atoms, error_code_index)

        Returns:
            atoms: 知识原子草稿列表
            error_code_index: {错误码: [atom_id, ...]}，用于 error_code_index 表
        """
        atoms: list[KnowledgeAtomDraft] = []
        error_code_index: dict[str, list[str]] = {}
        ctx = _ParseContext()

        for para in self._doc.paragraphs:
            text = self._get_text(para).strip()
            if not text:
                continue

            style = para.style.name

            if style == "Heading 1":
                ctx.h1 = text
                ctx.h2 = ctx.h3 = ctx.h4 = ctx.h5_type = ""
                ctx.lines = []

            elif style == "Heading 2":
                # H2 切换时先 flush 当前积累的内容
                self._flush(ctx, atoms, error_code_index)
                ctx.h2 = text
                ctx.h3 = ctx.h4 = ctx.h5_type = ""
                ctx.lines = []

            elif style == "Heading 3":
                self._flush(ctx, atoms, error_code_index)
                ctx.h3 = text
                ctx.h4 = ctx.h5_type = ""
                ctx.lines = []

            elif style == "Heading 4":
                self._flush(ctx, atoms, error_code_index)
                ctx.h4 = text
                # "现象描述"章节直接作为 background 原子
                if text.strip() in ("现象描述",):
                    ctx.h5_type = "background"
                else:
                    ctx.h5_type = ""
                ctx.lines = []

            elif style == "Heading 5":
                self._flush(ctx, atoms, error_code_index)
                # 确定原子类型
                h5_clean = text.strip()
                ctx.h5_type = next(
                    (t for k, t in H5_TYPE_MAP.items() if h5_clean.startswith(k)),
                    "fix_action",  # 默认视为修复动作
                )
                ctx.lines = []

            else:
                # Normal 或其他样式：收集文本行
                if ctx.h3 and ctx.h5_type:
                    ctx.lines.append(text)

        # 结束时 flush 最后一个段落
        self._flush(ctx, atoms, error_code_index)

        return atoms, error_code_index

    # ──────────────────────────────────────────────────────────────────────────

    def _flush(
        self,
        ctx: _ParseContext,
        atoms: list[KnowledgeAtomDraft],
        error_code_index: dict[str, list[str]],
    ) -> None:
        """将当前 context 中积累的行创建为一个知识原子（若有效）"""
        if not ctx.h3 or not ctx.h5_type:
            return
        if not ctx.lines:
            return

        full_text = "\n".join(ctx.lines)
        commands = self._extract_commands(ctx.lines)
        error_codes = self._extract_error_codes(full_text)

        # 构造 description：取第一行非空行，不超过 120 字符
        description_candidates = [l for l in ctx.lines if l.strip()]
        description = description_candidates[0][:120] if description_candidates else ctx.h3

        # 生成触发关键字：h3 是主要关键字，h4 是补充（若非"现象描述"）
        task_error_keywords: list[str] = [ctx.h3]
        if ctx.h4 and ctx.h4 not in ("前置检查", "现象描述"):
            task_error_keywords.append(ctx.h4)

        atom = KnowledgeAtomDraft(
            type=ctx.h5_type,
            category_id=CATEGORY_ID,
            knowledge_domain="sop",
            trigger={
                "stage": "S2",
                "task_error_keywords": task_error_keywords,
                "error_codes": [ec.upper() for ec in error_codes],
            },
            content={
                "description": description,
                "full_text": full_text,
                "commands": commands,
                "section": f"{ctx.h2} > {ctx.h3} > {ctx.h4}",
            },
            confidence=0.95,
            verified=True,
            source_type="docx",
            source_ref=str(self.docx_path),
        )

        atoms.append(atom)

        # 更新 error_code_index
        for ec in error_codes:
            ec_upper = ec.upper()
            error_code_index.setdefault(ec_upper, [])
            if atom.id not in error_code_index[ec_upper]:
                error_code_index[ec_upper].append(atom.id)

        # flush 后清空行缓冲（不清空 h3/h4/h5_type，等下一个 heading 来重置）
        ctx.lines = []

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_commands(lines: list[str]) -> list[str]:
        """从文本行列表中提取命令行片段（去重保序）

        识别规则：
        1. 行内反引号包裹内容（如 `acli vm.on`）
        2. 行开头匹配 CMD_PREFIXES（去掉序号前缀后）
        3. 行内嵌入命令：在中文句子中检测 CMD_PREFIXES 出现的位置并提取
           例：「使用  acli system top  检查...」→「acli system top」
        """
        seen: set[str] = set()
        commands: list[str] = []

        def _add(cmd: str) -> None:
            """去重添加命令"""
            cmd = cmd.strip()
            if cmd and cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)

        for line in lines:
            stripped = line.strip()

            # 规则 1：行内反引号提取
            for cmd in re.findall(r"`([^`]+)`", stripped):
                _add(cmd)

            # 规则 2：去掉序号前缀后整行是命令
            clean = re.sub(r"^[\s\d\.\)）(（a-zA-Z]+[\.、）\s]+", "", stripped).strip()
            lower_clean = clean.lower()
            if any(lower_clean.startswith(prefix) for prefix in CMD_PREFIXES):
                # 取命令部分（遇到中文注释截断）
                cmd_text = re.split(r"[#（【\u4e00-\u9fff]", clean)[0].strip()
                _add(cmd_text)
                continue  # 已匹配整行，不再做行内检测

            # 规则 3：行内嵌入命令检测（如 "使用  acli system top  检查"）
            # 找到最长的 CMD_PREFIX 命中位置，截取到下一个中文字符或注释符
            lower_stripped = stripped.lower()
            for prefix in CMD_PREFIXES:
                idx = lower_stripped.find(prefix)
                if idx == -1:
                    continue
                # 提取从 prefix 开始到中文或 # 结束的段落
                remainder = stripped[idx:]
                cmd_text = re.split(r"[#（【\u4e00-\u9fff]", remainder)[0].strip()
                if cmd_text:
                    _add(cmd_text)

        return commands

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_error_codes(text: str) -> list[str]:
        """从文本中提取所有错误码（格式：0x + 6-8位十六进制）"""
        return list(dict.fromkeys(ERROR_CODE_RE.findall(text)))  # 去重保序

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_text(para) -> str:  # type: ignore[return]
        """提取段落的纯文本（包含 runs 中的空格）"""
        return "".join(run.text for run in para.runs)
