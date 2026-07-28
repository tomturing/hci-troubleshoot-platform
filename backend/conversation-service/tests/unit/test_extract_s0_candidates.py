"""
ConversationService._extract_s0_candidates 单元测试

测试场景：
1. metadata 结构化提取（优先路径）
   - 直接 candidates 字段
   - event.metadata.candidates 嵌套结构
   - 空 metadata
   - candidates 字段缺失
2. 正则提取（兜底路径）
   - 正常候选格式
   - 包含括号等特殊字符
   - 多级分类前缀
   - 无匹配内容
3. S0 候选轮次管理
   - _get_s0_candidate_rounds
   - _increment_s0_candidate_rounds
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestExtractS0Candidates:
    """_extract_s0_candidates 方法测试"""

    @pytest.fixture
    def conversation_id(self) -> uuid.UUID:
        """测试对话 ID"""
        return uuid.UUID("00000000-0000-0000-0000-000000000001")

    @pytest.fixture
    def mock_service(self):
        """创建 ConversationService 模拟实例"""
        from app.services.conversation_service import ConversationService

        # 创建模拟的依赖
        mock_repo = MagicMock()
        mock_ai_registry = MagicMock()
        mock_session_factory = MagicMock()

        # 创建服务实例
        service = ConversationService(
            repository=mock_repo,
            ai_registry=mock_ai_registry,
            session_factory=mock_session_factory,
        )
        return service

    # ─── metadata 结构化提取测试 ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_extract_from_metadata_direct_candidates(self, mock_service, conversation_id):
        """从 metadata 直接 candidates 字段提取成功"""
        # 准备测试数据
        candidates_in_meta = [
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
            {"code": "存储-017", "name": "磁盘服务异常（直接对应告警类型）"},
        ]

        # 模拟 _get_last_assistant_message 返回
        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(
                "① 虚拟机-003 虚拟机开机失败",
                {"candidates": candidates_in_meta},
            ),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 2
        assert result[0]["code"] == "虚拟机-003"
        assert result[1]["name"] == "磁盘服务异常（直接对应告警类型）"

    @pytest.mark.asyncio
    async def test_extract_from_metadata_nested_candidates(self, mock_service, conversation_id):
        """从 event.metadata.candidates 嵌套结构提取成功"""
        candidates_in_meta = [
            {"code": "硬件-024", "name": "硬盘寿命到期"},
        ]

        metadata = {
            "kind": "interactive_request",
            "event": {
                "kind": "intent_selection",
                "metadata": {"candidates": candidates_in_meta},
            },
        }

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=("AI 消息内容", metadata),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 1
        assert result[0]["code"] == "硬件-024"

    @pytest.mark.asyncio
    async def test_extract_metadata_empty_fallback_to_regex(self, mock_service, conversation_id):
        """metadata 为空时退避到正则提取"""
        ai_content = "① 虚拟机-003 虚拟机开机失败\n② 存储-017 磁盘异常"

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(ai_content, None),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 2
        assert result[0]["code"] == "虚拟机-003"

    @pytest.mark.asyncio
    async def test_extract_metadata_missing_candidates_field(self, mock_service, conversation_id):
        """metadata 缺失 candidates 字段时退避到正则"""
        ai_content = "① 硬件-024 硬盘寿命到期"

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(ai_content, {"kind": "text"}),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 1

    # ─── 正则提取测试（兜底路径）────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_extract_regex_with_brackets(self, mock_service, conversation_id):
        """正则提取：分类名称包含括号"""
        ai_content = "① 存储-017 磁盘服务异常（直接对应告警类型）"

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(ai_content, None),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 1
        assert "磁盘服务异常（直接对应告警类型）" in result[0]["name"]

    @pytest.mark.asyncio
    async def test_extract_regex_multi_level_prefix(self, mock_service, conversation_id):
        """正则提取：多级分类前缀（含英文字母和数字）"""
        ai_content = "① 虚拟机-L2-001 虚拟机状态异常\n② 存储-L3-005 存储卷挂载失败"

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(ai_content, None),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 2
        assert result[0]["code"] == "虚拟机-L2-001"
        assert result[1]["code"] == "存储-L3-005"

    @pytest.mark.asyncio
    async def test_extract_regex_no_match(self, mock_service, conversation_id):
        """正则提取：无匹配内容"""
        ai_content = "这是一个普通的 AI 回复，没有候选列表"

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(ai_content, None),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert result == []

    @pytest.mark.asyncio
    async def test_extract_no_assistant_message(self, mock_service, conversation_id):
        """无最后一条 assistant 消息"""
        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert result == []

    @pytest.mark.asyncio
    async def test_extract_regex_five_candidates(self, mock_service, conversation_id):
        """正则提取：5 个候选（包含④⑤）"""
        ai_content = (
            "① 虚拟机-001 虚拟机开机失败\n"
            "② 存储-002 存储卷挂载异常\n"
            "③ 网络-003 网络连接失败\n"
            "④ 硬件-004 磁盘故障\n"
            "⑤ 以上都不是（请补充症状描述）"
        )

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=(ai_content, None),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        # 注意：⑤ 可能不会被正则匹配（因为格式不同），但④应该匹配
        assert len(result) >= 4

    @pytest.mark.asyncio
    async def test_extract_from_metadata_options_single_candidate(self, mock_service, conversation_id):
        """通过 metadata.options 结构化提取单候选成功"""
        metadata = {
            "kind": "choice_options",
            "options": [
                {"name": "硬件-024 硬盘寿命到期", "optionId": "1"},
                {"name": "以上不是，重新描述", "optionId": "2"},
            ],
        }

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=("AI 消息内容", metadata),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 1
        assert result[0]["code"] == "硬件-024"
        assert result[0]["name"] == "硬盘寿命到期"

    @pytest.mark.asyncio
    async def test_extract_from_metadata_options_multi_candidates(self, mock_service, conversation_id):
        """通过 metadata.options 结构化提取多候选成功"""
        metadata = {
            "kind": "choice_options",
            "options": [
                {"name": "虚拟机-003 虚拟机开机失败", "optionId": "1"},
                {"name": "存储-017 磁盘异常", "optionId": "2"},
                {"name": "以上都不是（请补充症状描述）", "optionId": "3"},
            ],
        }

        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=("AI 消息内容", metadata),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert len(result) == 2
        assert result[0]["code"] == "虚拟机-003"
        assert result[0]["name"] == "虚拟机开机失败"
        assert result[1]["code"] == "存储-017"
        assert result[1]["name"] == "磁盘异常"

    @pytest.mark.asyncio
    async def test_legacy_invalid_option_does_not_shift_original_ids(self, mock_service, conversation_id):
        """工单 Q2026072855923：过滤幻觉项后仍保留原 optionId。"""
        metadata = {
            "kind": "choice_options",
            "options": [
                {"optionId": "1", "name": "ubu-sus-25 "},
                {"optionId": "2", "name": "虚拟机-038 虚拟机IO读写慢"},
                {"optionId": "3", "name": "虚拟机-003 虚拟机开机失败"},
                {"optionId": "4", "name": "存储-020 虚拟存储性能告警"},
                {"optionId": "5", "name": "以上都不是（请补充症状描述）"},
            ],
        }
        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=("AI 消息内容", metadata),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert [candidate["option_id"] for candidate in result] == ["2", "3", "4"]
        assert result[1] == {
            "option_id": "3",
            "code": "虚拟机-003",
            "name": "虚拟机开机失败",
        }

    @pytest.mark.asyncio
    async def test_extract_v2_stable_category_options(self, mock_service, conversation_id):
        metadata = {
            "kind": "choice_options",
            "schemaVersion": 2,
            "options": [
                {
                    "optionId": "虚拟机-003",
                    "code": "虚拟机-003",
                    "categoryName": "虚拟机开机失败",
                    "name": "虚拟机-003 虚拟机开机失败",
                },
                {"optionId": "__none__", "name": "以上都不是（请补充症状描述）"},
            ],
        }
        with patch.object(
            mock_service,
            "_get_last_assistant_message",
            new_callable=AsyncMock,
            return_value=("AI 消息内容", metadata),
        ):
            result = await mock_service._extract_s0_candidates(conversation_id)

        assert result == [
            {
                "option_id": "虚拟机-003",
                "code": "虚拟机-003",
                "name": "虚拟机开机失败",
            }
        ]


class TestGetLastAssistantMessage:
    """_get_last_assistant_message 方法测试"""

    @pytest.fixture
    def conversation_id(self) -> uuid.UUID:
        return uuid.UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def mock_service(self):
        from app.services.conversation_service import ConversationService

        mock_repo = MagicMock()
        mock_ai_registry = MagicMock()
        mock_session_factory = MagicMock()

        service = ConversationService(
            repository=mock_repo,
            ai_registry=mock_ai_registry,
            session_factory=mock_session_factory,
        )
        return service

    @pytest.mark.asyncio
    async def test_get_message_with_session_factory(self, mock_service, conversation_id):
        """使用 session_factory 获取消息"""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = ("AI content in session", {"test_session": "data_session"})
        mock_result.fetchone.return_value = mock_row
        mock_session.execute.return_value = mock_result

        # Create an async context manager mock for session_factory
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__.return_value = mock_session
        mock_service.session_factory = MagicMock(return_value=mock_session_ctx)

        result = await mock_service._get_last_assistant_message(conversation_id)

        assert result[0] == "AI content in session"
        assert result[1] == {"test_session": "data_session"}
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_message_without_session_factory(self, mock_service, conversation_id):
        """使用 repository 获取消息"""
        mock_service.session_factory = None

        # 模拟消息对象
        mock_msg = MagicMock()
        mock_msg.content = "Test content"
        mock_msg.metadata = {"meta": "value"}
        mock_msg.role = MagicMock()
        mock_msg.role.value = "assistant"

        mock_service.repository.get_messages = AsyncMock(return_value=[mock_msg])

        result = await mock_service._get_last_assistant_message(conversation_id)

        assert result[0] == "Test content"
        assert result[1] == {"meta": "value"}

    @pytest.mark.asyncio
    async def test_get_message_no_messages(self, mock_service, conversation_id):
        """无 assistant 消息时返回 (None, None)"""
        mock_service.session_factory = None
        mock_service.repository.get_messages = AsyncMock(return_value=[])

        result = await mock_service._get_last_assistant_message(conversation_id)

        assert result == (None, None)


class TestS0CandidateRounds:
    """S0 候选轮次管理方法测试"""

    @pytest.fixture
    def conversation_id(self) -> uuid.UUID:
        return uuid.UUID("00000000-0000-0000-0000-000000000003")

    @pytest.fixture
    def mock_service(self):
        from app.services.conversation_service import ConversationService

        mock_repo = MagicMock()
        mock_ai_registry = MagicMock()
        mock_session_factory = MagicMock()

        service = ConversationService(
            repository=mock_repo,
            ai_registry=mock_ai_registry,
            session_factory=mock_session_factory,
        )
        return service

    @pytest.mark.asyncio
    async def test_get_s0_candidate_rounds_default(self, mock_service, conversation_id):
        """获取轮次数：默认返回 0"""
        mock_service.session_factory = None
        mock_conv = MagicMock()
        mock_conv.metadata_ = None
        mock_service.repository.get_conversation = AsyncMock(return_value=mock_conv)

        result = await mock_service._get_s0_candidate_rounds(conversation_id)

        assert result == 0

    @pytest.mark.asyncio
    async def test_get_s0_candidate_rounds_existing(self, mock_service, conversation_id):
        """获取轮次数：返回已有值"""
        mock_service.session_factory = None
        mock_conv = MagicMock()
        mock_conv.metadata_ = {"s0_candidate_rounds": 2}
        mock_service.repository.get_conversation = AsyncMock(return_value=mock_conv)

        result = await mock_service._get_s0_candidate_rounds(conversation_id)

        assert result == 2

    @pytest.mark.asyncio
    async def test_get_s0_candidate_rounds_no_conversation(self, mock_service, conversation_id):
        """获取轮次数：无对话记录时返回 0"""
        mock_service.session_factory = None
        mock_service.repository.get_conversation = AsyncMock(return_value=None)

        result = await mock_service._get_s0_candidate_rounds(conversation_id)

        assert result == 0

    @pytest.mark.asyncio
    async def test_get_s0_candidate_rounds_exception(self, mock_service, conversation_id):
        """获取轮次数：异常时返回 0"""
        mock_service.session_factory = None
        mock_service.repository.get_conversation = AsyncMock(side_effect=Exception("DB error"))

        result = await mock_service._get_s0_candidate_rounds(conversation_id)

        assert result == 0


class TestConversationManagerResolveCandidate:
    """ConversationManager.resolve_candidate_category 方法测试"""

    @pytest.fixture
    def manager(self):
        from app.services.conversation_manager import ConversationManager

        return ConversationManager()

    def test_resolve_candidate_selection_1(self, manager):
        """用户选择 ①"""
        candidates = [
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
            {"code": "存储-017", "name": "磁盘异常"},
        ]
        result = manager.resolve_candidate_category(1, candidates)

        assert result is not None
        assert result["code"] == "虚拟机-003"

    def test_resolve_candidate_selection_2(self, manager):
        """用户选择 ②"""
        candidates = [
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
            {"code": "存储-017", "name": "磁盘异常"},
        ]
        result = manager.resolve_candidate_category(2, candidates)

        assert result is not None
        assert result["code"] == "存储-017"

    def test_resolve_candidate_selection_3_none(self, manager):
        """用户选择 ③「以上都不是」"""
        candidates = [
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
            {"code": "存储-017", "name": "磁盘异常"},
        ]
        result = manager.resolve_candidate_category(3, candidates)

        assert result is None

    def test_resolve_candidate_out_of_range(self, manager):
        """用户选择超出范围"""
        candidates = [
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
        ]
        result = manager.resolve_candidate_category(5, candidates)

        assert result is None

    def test_resolve_candidate_empty_list(self, manager):
        """候选列表为空"""
        result = manager.resolve_candidate_category(1, [])

        assert result is None

    def test_resolve_legacy_choice_by_original_option_id(self, manager):
        candidates = [
            {"option_id": "2", "code": "虚拟机-038", "name": "虚拟机IO读写慢"},
            {"option_id": "3", "code": "虚拟机-003", "name": "虚拟机开机失败"},
            {"option_id": "4", "code": "存储-020", "name": "虚拟存储性能告警"},
        ]

        assert manager.resolve_candidate_category(3, candidates) == {
            "code": "虚拟机-003",
            "name": "虚拟机开机失败",
        }

    def test_resolve_v2_choice_by_stable_category_code(self, manager):
        candidates = [
            {
                "option_id": "虚拟机-003",
                "code": "虚拟机-003",
                "name": "虚拟机开机失败",
            }
        ]

        assert manager.resolve_candidate_option("虚拟机-003", candidates) == {
            "code": "虚拟机-003",
            "name": "虚拟机开机失败",
        }
        assert manager.resolve_candidate_option("__none__", candidates) is None
        assert manager.resolve_candidate_option("存储-020", candidates) is None
