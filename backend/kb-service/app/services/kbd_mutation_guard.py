"""KBD 内容写入门禁。

所有会改变 ``kbd_entry`` 知识内容的入口都必须复用这里的状态判断。路由层的前置
检查负责快速反馈；异步任务最终写回前再次检查，负责关闭“提交时是草稿、执行期间
被发布”的竞态窗口。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kbd_entry import KbdEntry


class PublishedKbdMutationError(RuntimeError):
    """试图绕过维护工作稿直接修改已发布 KBD。"""

    def __init__(self, kbd_id: int):
        self.kbd_id = kbd_id
        super().__init__(
            "已发布 KBD 不能直接重算或覆盖；请先创建维护工作稿，Agent 当前生效版保持不变"
        )


async def require_mutable_kbd(
    session: AsyncSession,
    kbd_id: int,
    *,
    for_update: bool = False,
) -> KbdEntry:
    """返回允许直接修改的 KBD；不存在或已发布时显式失败。

    ``for_update`` 只用于最终写回事务。前置检查不持有数据库锁，避免 LLM 长调用占用
    连接和行锁；写回检查使用 ``FOR UPDATE`` 保证状态判断与内容更新处于同一事务。
    """

    statement = select(KbdEntry).where(KbdEntry.id == kbd_id)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    entry = result.scalar_one_or_none()
    if entry is None:
        raise LookupError(f"KBD 条目 {kbd_id} 不存在")
    if entry.status == "published":
        raise PublishedKbdMutationError(kbd_id)
    return entry
