"""
单元测试：SOP 变量提分布尔值交互选项确认逻辑
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.sop_execution import VariableResponseRequest, sop_variable_response
from app.services.conversation_service import ConversationService


class TestSopBooleanConfirm:
    """测试 SOP 布尔类型变量的确认与提交"""

    @pytest.fixture
    def conversation_id(self) -> uuid.UUID:
        return uuid.UUID("00000000-0000-0000-0000-000000000100")

    @pytest.fixture
    def mock_service(self):
        """创建 mock 的 ConversationService 实例"""
        mock_repo = MagicMock()
        mock_ai_registry = MagicMock()
        mock_kb_client = AsyncMock()
        mock_session_factory = MagicMock()
        mock_agent_client = MagicMock()

        service = ConversationService(
            repository=mock_repo,
            ai_registry=mock_ai_registry,
            kb_client=mock_kb_client,
            session_factory=mock_session_factory,
            agent_client=mock_agent_client,
        )
        return service

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "option_id, expected_written",
        [
            ("true", "true"),
            ("false", "false"),
        ],
    )
    async def test_submit_interactive_response_boolean_selected(
        self, mock_service, conversation_id, option_id, expected_written
    ):
        """测试 submit_interactive_response 提交选项按钮"""
        # Mock SOP document schema
        mock_service.kb_client.get_sop_document = AsyncMock(
            return_value={
                "id": 2,
                "variable_schema": [
                    {"name": "is_sys_disk", "type": "boolean"},
                ],
            }
        )

        # Mock SopExecutionRepository
        mock_repo_instance = MagicMock()
        mock_repo_instance.get_by_conversation = AsyncMock(
            return_value=MagicMock(sop_document_id=2)
        )
        mock_repo_instance.set_variable = AsyncMock(return_value=MagicMock())

        # Mock sqlalchemy Session
        mock_session = AsyncMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__.return_value = mock_session
        mock_service.session_factory = MagicMock(return_value=mock_session_ctx)

        # Mock get_conversation
        mock_service.repository.get_conversation = AsyncMock(
            return_value=MagicMock(case_id="Q123")
        )
        mock_service.repository.add_message = AsyncMock()

        with patch("app.services.conversation_service.SopExecutionRepository", return_value=mock_repo_instance):
            success = await mock_service.submit_interactive_response(
                conversation_id=conversation_id,
                kind="variable_confirm",
                request_id="req-123",
                acp_session_id="acp-123",
                outcome={"outcome": "selected", "optionId": option_id},
                metadata={"variable_name": "is_sys_disk"},
            )

        assert success is True
        # 验证写入的值是否为正确传来的 optionId
        mock_repo_instance.set_variable.assert_called_once_with(
            conversation_id=conversation_id,
            variable_name="is_sys_disk",
            value=expected_written,
            source="user_input",
        )

    @pytest.mark.asyncio
    async def test_submit_interactive_response_string_selected(self, mock_service, conversation_id):
        """测试 string 类型变量选择提交"""
        mock_service.kb_client.get_sop_document = AsyncMock(
            return_value={
                "id": 2,
                "variable_schema": [
                    {"name": "other_str", "type": "string"},
                ],
            }
        )

        mock_repo_instance = MagicMock()
        mock_repo_instance.get_by_conversation = AsyncMock(
            return_value=MagicMock(sop_document_id=2)
        )
        mock_repo_instance.set_variable = AsyncMock(return_value=MagicMock())

        # Mock sqlalchemy Session
        mock_session = AsyncMock()
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__.return_value = mock_session
        mock_service.session_factory = MagicMock(return_value=mock_session_ctx)

        mock_service.repository.get_conversation = AsyncMock(
            return_value=MagicMock(case_id="Q123")
        )
        mock_service.repository.add_message = AsyncMock()

        with patch("app.services.conversation_service.SopExecutionRepository", return_value=mock_repo_instance):
            success = await mock_service.submit_interactive_response(
                conversation_id=conversation_id,
                kind="variable_confirm",
                request_id="req-123",
                acp_session_id="acp-123",
                outcome={"outcome": "selected", "optionId": "192.168.1.100"},
                metadata={"variable_name": "other_str"},
            )

        assert success is True
        mock_repo_instance.set_variable.assert_called_once_with(
            conversation_id=conversation_id,
            variable_name="other_str",
            value="192.168.1.100",
            source="user_input",
        )


@pytest.mark.asyncio
async def test_sop_variable_response_route_boolean():
    """测试 sop_variable_response 路由处理函数的布尔值设置"""
    conversation_id = uuid.UUID("00000000-0000-0000-0000-000000000200")
    request = MagicMock()

    # Mock variables_schema and database session
    mock_schema = [{"name": "is_sys_disk", "type": "boolean"}]
    mock_execution = MagicMock(sop_document_id=2, status="interrupted", pending_variable_name="is_sys_disk")

    mock_repo = MagicMock()
    mock_repo.get_by_conversation = AsyncMock(return_value=mock_execution)
    mock_repo.set_variable = AsyncMock(return_value=MagicMock(status="active"))

    mock_session = AsyncMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_factory = MagicMock(return_value=mock_session_ctx)

    mock_db_manager = MagicMock()
    mock_db_manager.async_session_factory = mock_session_factory

    body = VariableResponseRequest(
        variable_name="is_sys_disk",
        value="true",
        source="user_input",
    )

    with patch("app.routes.sop_execution._db_manager", mock_db_manager), \
         patch("app.routes.sop_execution._check_auth"), \
         patch("app.routes.sop_execution._get_variable_schema", AsyncMock(return_value=mock_schema)), \
         patch("app.routes.sop_execution.SopExecutionRepository", return_value=mock_repo):

        response = await sop_variable_response(
            request=request,
            conversation_id=conversation_id,
            body=body,
        )

    # 验证返回值中是否已成功设置为 "true"
    assert response.ok is True
    assert response.value == "true"
    mock_repo.set_variable.assert_called_once_with(
        conversation_id=conversation_id,
        variable_name="is_sys_disk",
        value="true",
        source="user_input",
    )
