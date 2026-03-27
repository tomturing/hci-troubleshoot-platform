"""
Conversation Service 测试 conftest — 激活 conversation-service 的 app 命名空间

提供全局 fixture:
  - setup_test_cases: 插入测试工单和用户数据（module 级）
  - async_client: HTTPX AsyncClient，用于集成测试
  - db_session: SQLAlchemy AsyncSession fixture，用于 DB 验证
"""

import os
import sys

import pytest

_svc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_expect = os.path.normpath(os.path.join(_svc_root, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    if _svc_root in sys.path:
        sys.path.remove(_svc_root)
    sys.path.insert(0, _svc_root)


@pytest.fixture(scope="module", autouse=True)
async def setup_test_cases():
    """
    在模块开始前插入必要的测试 Case 和 User 数据。

    注意：此 fixture 需要真实数据库连接。如果 DATABASE_URL 不可达，测试将跳过。
    """
    import logging

    from app.config import settings
    from shared.database.postgres import DatabaseManager
    from sqlalchemy import text

    logger = logging.getLogger(__name__)

    # 检查数据库是否可达
    test_db = DatabaseManager(settings.DATABASE_URL)
    try:
        # 尝试获取 session 来测试连接
        async for session in test_db.get_session():
            await session.execute(text("SELECT 1"))
            break
    except Exception as e:
        logger.warning(f"数据库不可达，跳过 setup_test_cases: {e}")
        yield
        await test_db.close()
        return

    test_uuids = [
        "1e106d60-0fe5-4c00-9421-1c4da35d128c",
        "0ceb21a2-2da6-449f-bbf7-f43d515b2d7c",
        "3fd03725-d003-4354-be46-6f4370beca8d",
        "971bfb12-f3d0-4680-91e6-1415e26be8ca",
        "6ba79191-6cff-4f80-a0d0-327f1e1ae98f",
        "6b9a8f4c-3f2d-4c0e-8f2c-5c4d3b8f1a9e",
        "e3b0c442-989b-464c-8693-b0a8c4f9a5e1",
        "5f187313-2d2c-493a-814a-59424d8622f9",
    ]

    test_cases = [
        {"case_id": "Q202602220001", "trace_id": "inttest-conv-001"},
        {"case_id": "Q202602220002", "trace_id": "inttest-conv-002"},
        {"case_id": "Q202602220003", "trace_id": "inttest-conv-003"},
        {"case_id": "Q202602220004", "trace_id": "inttest-conv-004"},
        {"case_id": "Q202602220005", "trace_id": "inttest-conv-005"},
    ]

    for i, test_case in enumerate(test_cases):
        test_uuid = test_uuids[i]

        async for session in test_db.get_session():
            # 插入必需的测试用户，使用 test_uuid 作为 client_id 保证唯一性
            await session.execute(
                text("""
                    INSERT INTO "user" (user_id, client_id, username, trace_id)
                    VALUES (:uid, :client_id, 'test-user-int', 'inttest-setup')
                    ON CONFLICT (user_id) DO NOTHING
                """),
                {"uid": test_uuid, "client_id": f"test-client-{test_uuid}"},
            )

            # 再插入测试工单
            await session.execute(
                text("""
                    INSERT INTO "case" (case_id, title, status, client_id, user_id, trace_id)
                    VALUES (:cid, 'Integration Test Case', 'created', :client_id, :uid, :tid)
                    ON CONFLICT (case_id) DO NOTHING
                """),
                {
                    "cid": test_case["case_id"],
                    "client_id": f"test-client-{test_uuid}",
                    "uid": test_uuid,
                    "tid": test_case["trace_id"],
                },
            )
            await session.commit()

    yield
    await test_db.close()


@pytest.fixture
async def async_client():
    """
    HTTPX AsyncClient fixture，用于集成测试。

    自动注入 app 的 lifespan_context，确保应用状态正确初始化。
    """
    import httpx
    from app.main import app
    from httpx import ASGITransport

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client


@pytest.fixture
async def db_session():
    """
    SQLAlchemy AsyncSession fixture，用于测试中的 DB 验证。

    使用 yield 返回 session factory，调用方需 async with db_session() 获取 session。
    """
    from app.config import settings
    from shared.database.postgres import DatabaseManager

    db_manager = DatabaseManager(settings.DATABASE_URL)

    try:
        async for session in db_manager.get_session():
            yield session
            await session.rollback()
    except Exception as e:
        pytest.skip(f"数据库不可用，跳过测试：{e}")
    finally:
        await db_manager.close()
