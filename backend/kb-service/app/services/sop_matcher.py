"""
SOP Matcher — 关键字精确路由

设计说明：
- SOP（Standard Operating Procedure）排障手册被视为"技能"（Skill）
- 每个 Skill 目录下有 keywords_map.json，定义关键字→章节的映射
- 匹配算法：将用户问题与 keywords 做精确子串匹配（大小写/简繁不敏感）
- 优先级高于向量/BM25 检索：命中 SOP 节点后，直接将章节 content 注入上下文

目录结构：
    sop_skills/
    ├── registry.json           # SOP 技能注册表（skill_id → 目录名）
    └── vm_boot_failure/
        ├── keywords_map.json   # 关键字 → 章节映射
        └── chapters/
            ├── 01_前置检查.md
            └── 04_CPU不足.md

注意事项：
- 启动时通过 load() 从文件系统加载，运行时不重新读取文件（热更新需重启）
- 关键字匹配是 O(n*m)，n=用户问题长度，m=总关键字数，可接受（< 1000 个关键字）
- 同时命中多个节点时，返回最长匹配的关键字对应的节点（specificity 优先）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from shared.observability.logger import get_logger

logger = get_logger("kb-service-sop")


class SopMatchResult:
    """SOP 匹配结果"""

    def __init__(
        self,
        skill_id: str,
        node_name: str,
        matched_keyword: str,
        content: str,
        file_path: str,
    ):
        self.skill_id = skill_id
        self.node_name = node_name
        self.matched_keyword = matched_keyword
        self.content = content
        self.file_path = file_path

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "node_name": self.node_name,
            "matched_keyword": self.matched_keyword,
            "content": self.content,
            "file_path": self.file_path,
        }


class SopMatcher:
    """SOP 关键字精确路由器

    启动时从 sop_skills/ 目录加载所有 keywords_map.json，
    构建内存索引 keyword → (skill_id, node_name, file_path)。
    """

    def __init__(self, sop_skills_dir: str):
        self._sop_skills_dir = Path(sop_skills_dir)
        # keyword（小写）→ (skill_id, node_name, file_path)
        self._index: dict[str, tuple[str, str, str]] = {}
        self._loaded = False

    async def load(self) -> None:
        """从 sop_skills/ 目录加载所有 keywords_map.json"""
        if not self._sop_skills_dir.exists():
            logger.warning(
                event="sop_dir_not_found",
                message=f"SOP 技能目录不存在: {self._sop_skills_dir}，SOP 匹配将始终返回 None",
            )
            self._loaded = True
            return

        loaded_skills = 0
        loaded_keywords = 0

        for skill_dir in self._sop_skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue

            keywords_map_path = skill_dir / "keywords_map.json"
            if not keywords_map_path.exists():
                continue

            skill_id = skill_dir.name
            try:
                with open(keywords_map_path, encoding="utf-8") as f:
                    keywords_map: dict = json.load(f)

                for node_name, node_info in keywords_map.items():
                    file_name = node_info.get("file", "")
                    file_path = str(skill_dir / "chapters" / file_name) if file_name else ""

                    # 支持嵌套子章节（subchapters）
                    all_keywords = list(node_info.get("keywords", []))
                    subchapters = node_info.get("subchapters", {})
                    for sub_name, sub_info in subchapters.items():
                        sub_file = sub_info.get("file", "")
                        sub_path = str(skill_dir / "chapters" / sub_file) if sub_file else file_path
                        for kw in sub_info.get("keywords", []):
                            self._index[kw.lower()] = (skill_id, sub_name, sub_path)
                            loaded_keywords += 1

                    for kw in all_keywords:
                        # 子章节关键字精度更高，不覆盖已有条目
                        if kw.lower() not in self._index:
                            self._index[kw.lower()] = (skill_id, node_name, file_path)
                            loaded_keywords += 1

                loaded_skills += 1
            except (json.JSONDecodeError, OSError) as exc:
                logger.error(event="sop_load_error", skill_id=skill_id, error=str(exc))

        self._loaded = True
        logger.info(
            event="sop_loaded",
            message=f"SOP 索引加载完成：{loaded_skills} 个技能，{loaded_keywords} 个关键字",
            skills=loaded_skills,
            keywords=loaded_keywords,
        )

    def match(self, query: str) -> SopMatchResult | None:
        """在查询文本中精确匹配 SOP 关键字

        Args:
            query: 用户问题文本

        Returns:
            SopMatchResult（命中时）或 None（未命中时）
        """
        if not self._loaded:
            logger.warning(event="sop_not_loaded", message="SOP 索引尚未加载")
            return None

        query_lower = query.lower()
        best_match: tuple[str, str, str] | None = None
        best_kw = ""

        # 遍历所有关键字，找到最长匹配（specificity 优先）
        for kw, match_info in self._index.items():
            if kw in query_lower and len(kw) > len(best_kw):
                best_kw = kw
                best_match = match_info

        if not best_match:
            return None

        skill_id, node_name, file_path = best_match

        # 读取节点内容（从文件系统）
        content = ""
        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, encoding="utf-8") as f:
                    content = f.read()
            except OSError as exc:
                logger.warning(event="sop_file_read_error", file_path=file_path, error=str(exc))

        logger.info(
            event="sop_matched",
            query=query[:50],
            skill_id=skill_id,
            node_name=node_name,
            keyword=best_kw,
        )
        return SopMatchResult(
            skill_id=skill_id,
            node_name=node_name,
            matched_keyword=best_kw,
            content=content,
            file_path=file_path,
        )

    @property
    def index_size(self) -> int:
        """返回已加载的关键字数量"""
        return len(self._index)
