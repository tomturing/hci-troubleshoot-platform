"""
Unit Tests for Conversation Service
"""

import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import uuid
from datetime import datetime
import asyncio
import sys
import os
# Add backend/conversation-service to path for 'app' imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
# Add backend to path for 'shared' imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.services.conversation_service import ConversationService
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole

class TestConversationService(unittest.IsolatedAsyncioTestCase):
    
    def setUp(self):
        self.mock_repo = MagicMock()
        self.mock_client = MagicMock()
        self.service = ConversationService(self.mock_repo, self.mock_client)
        
    async def test_create_conversation(self):
        # Arrange
        case_id = "test-case-123"
        trace_id = "test-trace-123"
        expected_conv = Conversation(
            conversation_id=uuid.uuid4(),
            case_id=case_id,
            trace_id=trace_id
        )
        self.mock_repo.create_conversation = AsyncMock(return_value=expected_conv)
        
        # Act
        result = await self.service.create_conversation(case_id, trace_id)
        
        # Assert
        self.assertEqual(result, expected_conv)
        self.mock_repo.create_conversation.assert_called_once_with(
            case_id=case_id,
            trace_id=trace_id,
            metadata=None
        )

    async def test_send_message_flow(self):
        # Arrange
        conversation_id = uuid.uuid4()
        case_id = "test-case-123"
        content = "Hello, AI"
        trace_id = "test-trace-123"
        
        # Mock add_message (user)
        self.mock_repo.add_message = AsyncMock()
        
        # Mock get_messages (history)
        history = [
            Message(role=MessageRole.user, content="Hi"),
            Message(role=MessageRole.assistant, content="Hello")
        ]
        self.mock_repo.get_messages = AsyncMock(return_value=history)
        
        # Mock OpenClaw client stream
        async def mock_stream(*args, **kwargs):
            yield "I am "
            yield "OpenClaw"
            
        self.mock_client.chat_completion_stream = mock_stream
        
        # Act
        chunks = []
        async for chunk in self.service.send_message(conversation_id, case_id, content, trace_id):
            chunks.append(chunk)
            
        # Assert
        self.assertEqual("".join(chunks), "I am OpenClaw")
        
        # Verify user message saved
        self.mock_repo.add_message.assert_any_call(
            conversation_id=conversation_id,
            case_id=case_id,
            role=MessageRole.user,
            content=content,
            trace_id=trace_id
        )
        
        # Verify history fetching
        self.mock_repo.get_messages.assert_called_once_with(conversation_id)
        
        # Verify AI message saved (accumulated)
        self.mock_repo.add_message.assert_any_call(
            conversation_id=conversation_id,
            case_id=case_id,
            role=MessageRole.assistant,
            content="I am OpenClaw",
            trace_id=trace_id
        )

if __name__ == '__main__':
    unittest.main()
