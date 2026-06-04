"""
Unit Tests for SkillDefinition ORM model and routes
"""

import sys

# 隔离 conversation-service app 命名空间（backend/ 已通过 pyproject.toml pythonpath 全局配置）
_svc = "/mnt/d/aihci/hci-troubleshoot-platform/backend/conversation-service"

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
            "display_name",
            "description",
            "parameters_schema",
            "output_schema",
            "is_active",
            "version",
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
            skill_name="disk_vendor_lifetime",
            display_name="硬盘厂商识别与寿命判定",
            description="根据 SMART 信息判断硬盘厂商和寿命",
            parameters_schema={},
            output_schema={},
            is_active=True,
            version="1.0",
        )
        repr_str = repr(inst)
        assert "skill_name='disk_vendor_lifetime'" in repr_str
        assert "display_name='硬盘厂商识别与寿命判定'" in repr_str


class TestSkillDefinitionRoutes:
    """测试 skill_definition CRUD 路由"""

    def test_router_prefix(self):
        """router 使用正确前缀"""
        from app.routes.skill_definition import router

        assert router.prefix == "/api/v1/skills"
