"""
AtomWriter — 将 KnowledgeAtomDraft 批量写入 PostgreSQL

通过 asyncpg 直接执行 INSERT（绕过 ORM），减少多次往返开销。
同步写入 error_code_index 表（UPSERT 合并 knowledge_atom_ids 数组）。

使用示例::

    writer = AtomWriter(database_url="postgresql://...")
    results = await writer.write(atoms, error_code_index)
    # results: {"atoms_written": 35, "error_codes_written": 8}
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from .docx_extractor import KnowledgeAtomDraft

logger = logging.getLogger(__name__)


class AtomWriter:
    """知识原子批量写入器

    原子操作语义：
      - knowledge_atoms 使用 INSERT … ON CONFLICT (id) DO UPDATE（幂等）
      - error_code_index 使用 INSERT … ON CONFLICT (error_code) DO UPDATE，
        合并 knowledge_atom_ids JSON 数组去重
    """

    def __init__(self, database_url: str) -> None:
        # 将 asyncpg 不支持的 +asyncpg driver 前缀转为标准 postgresql://
        self.database_url = database_url.replace(
            "postgresql+asyncpg://", "postgresql://"
        ).replace(
            "postgres+asyncpg://", "postgresql://"
        )

    async def write(
        self,
        atoms: list[KnowledgeAtomDraft],
        error_code_index: dict[str, list[str]],
        trace_id: str | None = None,
    ) -> dict[str, int]:
        """执行写入操作，返回写入结果统计"""
        conn = await asyncpg.connect(self.database_url)
        try:
            atoms_written = await self._write_atoms(conn, atoms, trace_id)
            ecs_written = await self._write_error_codes(conn, error_code_index)
        finally:
            await conn.close()

        logger.info(
            "AtomWriter 写入完成: atoms=%d, error_codes=%d trace_id=%s",
            atoms_written,
            ecs_written,
            trace_id,
        )
        return {"atoms_written": atoms_written, "error_codes_written": ecs_written}

    # ──────────────────────────────────────────────────────────────────────────

    async def _write_atoms(
        self,
        conn: asyncpg.Connection,
        atoms: list[KnowledgeAtomDraft],
        trace_id: str | None,
    ) -> int:
        """批量 UPSERT knowledge_atoms"""
        if not atoms:
            return 0

        sql = """
        INSERT INTO knowledge_atoms (
            id, type, category_id, knowledge_domain,
            trigger, content,
            confidence, verified,
            source_type, source_ref,
            usage_count, feedback_positive, feedback_negative,
            trace_id
        ) VALUES (
            $1, $2, $3, $4,
            $5::jsonb, $6::jsonb,
            $7, $8,
            $9, $10,
            0, 0, 0,
            $11
        )
        ON CONFLICT (id) DO UPDATE SET
            type              = EXCLUDED.type,
            category_id       = EXCLUDED.category_id,
            trigger           = EXCLUDED.trigger,
            content           = EXCLUDED.content,
            confidence        = EXCLUDED.confidence,
            verified          = EXCLUDED.verified,
            source_type       = EXCLUDED.source_type,
            source_ref        = EXCLUDED.source_ref,
            updated_at        = NOW()
        """

        rows = [
            (
                atom.id,
                atom.type,
                atom.category_id,
                atom.knowledge_domain,
                json.dumps(atom.trigger, ensure_ascii=False),
                json.dumps(atom.content, ensure_ascii=False),
                atom.confidence,
                atom.verified,
                atom.source_type,
                atom.source_ref,
                trace_id,
            )
            for atom in atoms
        ]

        await conn.executemany(sql, rows)
        logger.debug("写入 %d 个知识原子", len(atoms))
        return len(atoms)

    # ──────────────────────────────────────────────────────────────────────────

    async def _write_error_codes(
        self,
        conn: asyncpg.Connection,
        error_code_index: dict[str, list[str]],
    ) -> int:
        """UPSERT error_code_index，合并 knowledge_atom_ids"""
        if not error_code_index:
            return 0

        sql = """
        INSERT INTO error_code_index (error_code, knowledge_atom_ids, source)
        VALUES ($1, $2::jsonb, 'auto_extracted')
        ON CONFLICT (error_code) DO UPDATE SET
            knowledge_atom_ids = (
                SELECT jsonb_agg(DISTINCT elem)
                FROM jsonb_array_elements(
                    COALESCE(error_code_index.knowledge_atom_ids, '[]'::jsonb)
                    || EXCLUDED.knowledge_atom_ids
                ) AS elem
            ),
            updated_at = NOW()
        """

        rows = [
            (ec, json.dumps(atom_ids, ensure_ascii=False))
            for ec, atom_ids in error_code_index.items()
        ]

        await conn.executemany(sql, rows)
        logger.debug("写入 %d 个错误码索引", len(error_code_index))
        return len(error_code_index)
