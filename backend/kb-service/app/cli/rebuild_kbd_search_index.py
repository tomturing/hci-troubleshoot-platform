"""重建已发布 KBD 的中文 FTS 与 embedding provenance。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from datetime import UTC, datetime

from shared.database.postgres import DatabaseManager
from sqlalchemy import text

from app.config import settings
from app.models.kbd_entry import build_kbd_embedding_text, strip_markdown
from app.services.embedding import EmbeddingService
from app.utils.jieba_hci import init_jieba, segment


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重建已发布 KBD 的检索索引")
    parser.add_argument(
        "--with-embeddings",
        action="store_true",
        help="同时调用真实 embedding provider 重建全部向量；默认仅重建中文 FTS",
    )
    return parser.parse_args()


async def rebuild(*, with_embeddings: bool) -> tuple[int, int, int]:
    """返回处理数、向量成功数、向量失败数。"""
    init_jieba()
    db = DatabaseManager(settings.DATABASE_URL)
    embedding_service = EmbeddingService(settings)
    processed = 0
    embedding_succeeded = 0
    embedding_failed = 0

    try:
        async with db.async_session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, title, problem_description, alert_info, root_cause, content_md, content_raw
                    FROM kbd_entry
                    WHERE status = 'published'
                    ORDER BY id
                    """
                )
            )
            rows = result.mappings().all()

        for row in rows:
            content_raw = strip_markdown(row["content_md"] or "")
            tsv_text = segment(f"{row['title']} {content_raw}")
            embedding_text = build_kbd_embedding_text(
                title=row["title"],
                problem_description=row["problem_description"],
                alert_info=row["alert_info"],
                root_cause=row["root_cause"],
                fallback_text=row["content_raw"] or row["content_md"],
            )
            vector: list[float] | None = None
            if with_embeddings:
                try:
                    vector = await embedding_service.embed_single(embedding_text)
                    embedding_succeeded += 1
                except RuntimeError:
                    embedding_failed += 1

            params = {
                "id": row["id"],
                "content_raw": content_raw,
                "tsv_text": tsv_text,
                "embedding": "[" + ",".join(str(value) for value in vector) + "]" if vector else None,
                "embedding_model": embedding_service.model_name if vector else None,
                "embedding_content_hash": (
                    hashlib.sha256(embedding_text.encode("utf-8")).hexdigest() if vector else None
                ),
                "embedding_updated_at": datetime.now(UTC) if vector else None,
                "update_embedding": with_embeddings,
            }
            async with db.async_session_factory() as session:
                await session.execute(
                    text(
                        """
                        UPDATE kbd_entry
                        SET content_raw = :content_raw,
                            tsv = to_tsvector('simple', :tsv_text),
                            embedding = CASE
                                WHEN :update_embedding THEN CAST(:embedding AS vector)
                                ELSE embedding
                            END,
                            embedding_model = CASE
                                WHEN :update_embedding THEN :embedding_model
                                ELSE embedding_model
                            END,
                            embedding_content_hash = CASE
                                WHEN :update_embedding THEN :embedding_content_hash
                                ELSE embedding_content_hash
                            END,
                            embedding_updated_at = CASE
                                WHEN :update_embedding THEN :embedding_updated_at
                                ELSE embedding_updated_at
                            END
                        WHERE id = :id
                        """
                    ),
                    params,
                )
                await session.commit()
            processed += 1
    finally:
        await db.close()

    return processed, embedding_succeeded, embedding_failed


def main() -> None:
    """命令行入口。"""
    args = _parse_args()
    processed, embedding_succeeded, embedding_failed = asyncio.run(rebuild(with_embeddings=args.with_embeddings))
    print(
        f"KBD 检索索引重建完成: processed={processed}, "
        f"embedding_succeeded={embedding_succeeded}, embedding_failed={embedding_failed}"
    )


if __name__ == "__main__":
    main()
