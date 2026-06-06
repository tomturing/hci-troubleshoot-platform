"""
单元测试：SOP 变量提分布尔值自动归一化校验
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.sop_execution import VariableResponseRequest, sop_variable_response
from app.services.conversation_service import ConversationService


class TestSopBooleanCoercion:
    """测试 SOP 布尔类型变量的输入归一化"""

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
        "user_input, expected_coerced",
        [
            ("是", "true"),
            ("yes", "true"),
            ("true", "true"),
            ("1", "true"),
            ("对", "true"),
            ("否", "false"),
            ("no", "false"),
            ("false", "false"),
            ("0", "false"),
            ("错", "false"),
            ("other", "other"),  # 不在布尔列表中的保持原样
        ],
    )
    async def test_submit_interactive_response_boolean_coercion(
        self, mock_service, conversation_id, user_input, expected_coerced
    ):
        """测试 submit_interactive_response 布尔值归一化"""
        # Mock SOP document schema
        mock_service.kb_client.get_sop_document = AsyncMock(
            return_value={
                "id": 2,
                "variable_schema": [
                    {"name": "is_sys_disk", "type": "boolean"},
                    {"name": "other_str", "type": "string"},
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
                kind="variable_input",
                request_id="req-123",
                acp_session_id="acp-123",
                outcome={"outcome": "free_text", "text": user_input},
                metadata={"variable_name": "is_sys_disk"},
            )

        assert success is True
        # 验证写入的值是否正确归一化
        mock_repo_instance.set_variable.assert_called_once_with(
            conversation_id=conversation_id,
            variable_name="is_sys_disk",
            value=expected_coerced,
            source="user_input",
        )

    @pytest.mark.asyncio
    async def test_submit_interactive_response_string_no_coercion(self, mock_service, conversation_id):
        """测试 string 类型变量不进行布尔归一化"""
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
                kind="variable_input",
                request_id="req-123",
                acp_session_id="acp-123",
                outcome={"outcome": "free_text", "text": "是"},
                metadata={"variable_name": "other_str"},
            )

        assert success is True
        # string 类型的 "是" 应该原样写入，不被强制转为 "true"
        mock_repo_instance.set_variable.assert_called_once_with(
            conversation_id=conversation_id,
            variable_name="other_str",
            value="是",
            source="user_input",
        )


@pytest.mark.asyncio
async def test_sop_variable_response_route_coercion():
    """测试 sop_variable_response 路由处理函数的布尔值归一化"""
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
        value="是",
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

    # 验证返回值中是否已成功归一化为 "true"
    assert response.ok is True
    assert response.value == "true"
    mock_repo.set_variable.assert_called_once_with(
        conversation_id=conversation_id,
        variable_name="is_sys_disk",
        value="true",
        source="user_input",
    )
