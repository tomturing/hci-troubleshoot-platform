"""
Unit Tests for SkillDefinition ORM model and routes
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# 隔离 conversation-service app 命名空间（backend/ 已通过 pyproject.toml pythonpath 全局配置）
_svc = str(Path(__file__).resolve().parents[2])

if _svc not in sys.path:
    sys.path.insert(0, _svc)


class TestSkillDefinitionORM:
    """测试 SkillDefinition ORM 模型"""

    def test_tablename(self):
        """表名正确"""
        from app.models.skill_definition import SkillDefinition

        assert SkillDefinition.__tablename__ == "skill_definition"

    def test_required_columns(self):
        """必要列定义完整"""
        from app.models.skill_definition import SkillDefinition

        cols = {c.name for c in SkillDefinition.__table__.columns}
        expected = {
            "id",
            "skill_name",
            "description",
            "instructions_md",
            "compatibility",
            "license",
            "allowed_tools",
            "metadata_json",
            "display_name",
            "is_active",
            "assets_json",
            "references_json",
            # 注意：skill_definition 是静态知识定义表（类似代码文件），不需要 trace_id
            # trace_id 仅用于运行时动态数据表（message、fact、sop_execution 等）
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols), f"缺少列: {expected - cols}"

    def test_skill_name_unique(self):
        """skill_name 列设置 unique 约束"""
        from app.models.skill_definition import SkillDefinition

        col = SkillDefinition.__table__.columns["skill_name"]
        assert col.unique is True

    def test_repr(self):
        """__repr__ 正确返回"""
        from app.models.skill_definition import SkillDefinition

        inst = SkillDefinition(
            id=1,
            skill_name="disk-vendor-lifetime",
            display_name="硬盘厂商识别与寿命判定",
            description="根据 SMART 信息判断硬盘厂商和寿命",
            instructions_md="# Title",
            is_active=True,
        )
        repr_str = repr(inst)
        assert "skill_name='disk-vendor-lifetime'" in repr_str
        assert "display_name='硬盘厂商识别与寿命判定'" in repr_str


@pytest.fixture
def mock_session():
    """创建模拟数据库会话"""
    return AsyncMock(spec=AsyncSession)


class TestSkillDefinitionRoutes:
    """测试 skill_definition CRUD 路由与依赖注入"""

    def test_router_prefix(self):
        """router 使用正确前缀"""
        from app.routes.skill_definition import router

        assert router.prefix == "/api/v1/skills"

    @pytest.mark.asyncio
    async def test_get_db_uninitialized(self):
        """数据库管理器未初始化时 get_db 报错"""
        from app.routes.skill_definition import get_db, set_skill_database_manager
        from fastapi import HTTPException

        set_skill_database_manager(None)

        with pytest.raises(HTTPException) as exc_info:
            async for _ in get_db():
                pass
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_get_db_success(self):
        """数据库管理器正常初始化时 get_db 成功产生会话"""
        from app.routes.skill_definition import get_db, set_skill_database_manager

        mock_db = MagicMock()
        mock_sess = AsyncMock()

        async def mock_get_session():
            yield mock_sess

        mock_db.get_session = mock_get_session
        set_skill_database_manager(mock_db)

        sessions = []
        async for session in get_db():
            sessions.append(session)

        assert len(sessions) == 1
        assert sessions[0] == mock_sess

    @pytest.mark.asyncio
    async def test_list_skills(self, mock_session):
        """测试获取技能定义列表（支持 is_active 过滤）"""
        from app.routes.skill_definition import list_skills

        mock_skill = MagicMock()
        mock_skill.id = 1
        mock_skill.skill_name = "test-skill"
        mock_skill.display_name = "Test"
        mock_skill.description = "Desc"
        mock_skill.compatibility = "Compat"
        mock_skill.license = "MIT"
        mock_skill.allowed_tools = "tool1 tool2"
        mock_skill.metadata_json = {"category": "test"}
        mock_skill.is_active = True
        mock_skill.assets_json = []
        mock_skill.references_json = []
        mock_skill.created_at = None
        mock_skill.updated_at = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_skill]
        mock_session.execute.return_value = mock_result

        result = await list_skills(is_active=True, db=mock_session)
        assert len(result) == 1
        assert result[0]["skill_name"] == "test-skill"

    @pytest.mark.asyncio
    async def test_get_skill_success(self, mock_session):
        """测试获取特定技能定义详情成功"""
        from app.routes.skill_definition import get_skill

        mock_skill = MagicMock()
        mock_skill.id = 1
        mock_skill.skill_name = "test-skill"
        mock_skill.display_name = "Test"
        mock_skill.description = "Desc"
        mock_skill.instructions_md = "# Step 1"
        mock_skill.compatibility = "Compat"
        mock_skill.license = "MIT"
        mock_skill.allowed_tools = "tool1 tool2"
        mock_skill.metadata_json = {"category": "test"}
        mock_skill.is_active = True
        mock_skill.assets_json = []
        mock_skill.references_json = []
        mock_skill.created_at = None
        mock_skill.updated_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_skill
        mock_session.execute.return_value = mock_result

        result = await get_skill(skill_id=1, db=mock_session)
        assert result["skill_name"] == "test-skill"
        assert result["instructions_md"] == "# Step 1"

    @pytest.mark.asyncio
    async def test_get_skill_not_found(self, mock_session):
        """测试获取不存在的技能定义返回 404"""
        from app.routes.skill_definition import get_skill
        from fastapi import HTTPException

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await get_skill(skill_id=999, db=mock_session)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_create_skill_success(self, mock_session):
        """测试成功创建新技能定义"""
        from app.models.skill_definition import SkillDefinition
        from app.routes.skill_definition import create_skill

        mock_check_result = MagicMock()
        mock_check_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_check_result

        payload = {
            "skill_name": "new-skill",
            "display_name": "New Skill",
            "description": "Desc description of the skill trigger",
            "instructions_md": "# Step 1",
            "is_active": True,
        }

        result = await create_skill(payload=payload, db=mock_session)
        assert result["status"] == "success"
        added_rows = [call.args[0] for call in mock_session.add.call_args_list]
        assert any(isinstance(row, SkillDefinition) for row in added_rows)
        assert any(
            row.__class__.__name__ == "DynamicResourceRevision"
            and getattr(row, "resource_type", None) == "skill"
            and getattr(row, "resource_name", None) == "new-skill"
            for row in added_rows
        )
        assert result["resource_revision"]["resource_type"] == "skill"
        assert result["resource_revision"]["resource_name"] == "new-skill"
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_skill_missing_name(self, mock_session):
        """测试创建新技能定义缺少 skill_name 报错"""
        from app.routes.skill_definition import create_skill
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await create_skill(payload={}, db=mock_session)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_skill_name_conflict(self, mock_session):
        """测试创建新技能定义标识重名报错"""
        from app.routes.skill_definition import create_skill
        from fastapi import HTTPException

        mock_check_result = MagicMock()
        mock_check_result.scalar_one_or_none.return_value = MagicMock()
        mock_session.execute.return_value = mock_check_result

        with pytest.raises(HTTPException) as exc_info:
            await create_skill(
                payload={"skill_name": "conflict-name", "description": "some trigger description"}, db=mock_session
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_skill_success(self, mock_session):
        """测试更新技能定义字段成功"""
        from app.models.skill_definition import SkillDefinition
        from app.routes.skill_definition import update_skill

        mock_skill = SkillDefinition(
            id=1,
            skill_name="old-name",
            description="Old Desc description of trigger",
            instructions_md="# Step 1",
            compatibility=None,
            license=None,
            allowed_tools=None,
            metadata_json={},
            display_name="Old Name",
            is_active=True,
            assets_json=[],
            references_json=[],
        )

        mock_get_result = MagicMock()
        mock_get_result.scalar_one_or_none.return_value = mock_skill

        mock_revision_result = MagicMock()
        mock_revision_result.scalar_one_or_none.return_value = None

        mock_next_revision_result = MagicMock()
        mock_next_revision_result.scalar_one.return_value = 1

        mock_session.execute.side_effect = [
            mock_get_result,
            MagicMock(),
            mock_revision_result,
            mock_next_revision_result,
        ]

        payload = {
            "display_name": "New Name",
            "description": "New Desc description of trigger",
            "instructions_md": "# Step 2",
            "is_active": False,
        }

        result = await update_skill(skill_id=1, payload=payload, db=mock_session)
        assert result["status"] == "success"
        assert mock_skill.display_name == "New Name"
        assert mock_skill.description == "New Desc description of trigger"
        assert mock_skill.instructions_md == "# Step 2"
        assert mock_skill.is_active is False
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_skill_not_found(self, mock_session):
        """测试更新不存在的技能定义返回 404"""
        from app.routes.skill_definition import update_skill
        from fastapi import HTTPException

        mock_get_result = MagicMock()
        mock_get_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_get_result

        with pytest.raises(HTTPException) as exc_info:
            await update_skill(skill_id=999, payload={}, db=mock_session)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_toggle_skill_status(self, mock_session):
        """测试快速切换启用状态"""
        from app.models.skill_definition import SkillDefinition
        from app.routes.skill_definition import toggle_skill_status

        mock_skill = SkillDefinition(
            id=1,
            skill_name="toggle-skill",
            description="Toggle skill trigger description",
            instructions_md="# Step 1",
            compatibility=None,
            license=None,
            allowed_tools=None,
            metadata_json={},
            display_name="Toggle Skill",
            is_active=True,
            assets_json=[],
            references_json=[],
        )

        mock_get_result = MagicMock()
        mock_get_result.scalar_one_or_none.return_value = mock_skill

        mock_revision_result = MagicMock()
        mock_revision_result.scalar_one_or_none.return_value = None

        mock_next_revision_result = MagicMock()
        mock_next_revision_result.scalar_one.return_value = 1

        mock_session.execute.side_effect = [
            mock_get_result,
            MagicMock(),
            mock_revision_result,
            mock_next_revision_result,
        ]

        result = await toggle_skill_status(skill_id=1, db=mock_session)
        assert result["status"] == "success"
        assert result["is_active"] is False
        assert mock_skill.is_active is False
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_skill_success(self, mock_session):
        """测试删除技能定义成功"""
        from app.routes.skill_definition import delete_skill

        mock_delete_result = MagicMock()
        mock_delete_result.rowcount = 1
        mock_session.execute.return_value = mock_delete_result

        result = await delete_skill(skill_id=1, db=mock_session)
        assert result["status"] == "success"
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_skill_not_found(self, mock_session):
        """测试删除不存在的技能定义返回 404"""
        from app.routes.skill_definition import delete_skill
        from fastapi import HTTPException

        mock_delete_result = MagicMock()
        mock_delete_result.rowcount = 0
        mock_session.execute.return_value = mock_delete_result

        with pytest.raises(HTTPException) as exc_info:
            await delete_skill(skill_id=999, db=mock_session)
        assert exc_info.value.status_code == 404
