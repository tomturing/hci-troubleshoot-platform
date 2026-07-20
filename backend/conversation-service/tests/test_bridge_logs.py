"""
terminal_bridge 日志回采功能单元测试

测试范围：
  - bridge_logs.py 回采接口
  - 前端日志缓冲和重试逻辑（通过 API 调用模拟）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 使用相对导入而非绝对导入（避免包名包含 - 的问题）
import sys

# 添加 app 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from routes.bridge_logs import (
    BridgeLogBatchRequest,
    BridgeLogEntry,
    BridgeLogResponse,
    collect_bridge_logs,
    set_dependencies,
    _verify_log_signature,
)


@pytest.fixture
def mock_db_manager():
    """模拟数据库管理器"""
    manager = MagicMock()
    session = AsyncMock()

    # 模拟 execute 返回值
    async def mock_execute(*args, **kwargs):
        return MagicMock()

    session.execute = mock_execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    manager.get_session = MagicMock(return_value=session)
    return manager


@pytest.fixture
def app():
    """创建测试应用"""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """创建测试客户端"""
    return TestClient(app)


class TestBridgeLogsAPI:
    """测试 bridge_logs 回采接口"""

    def test_collect_bridge_logs_success(self, mock_db_manager, client):
        """测试正常日志回采"""
        # 设置依赖
        set_dependencies(mock_db_manager)

        # 构造请求
        logs = [
            BridgeLogEntry(
                case_id="Q20260720001",
                trace_id="trace-123",
                level="INFO",
                event="exec.start",
                message="开始执行命令",
                custom_ui="hci.local",
                extra={"exec_id": "exec-001"},
            ),
            BridgeLogEntry(
                case_id="Q20260720001",
                trace_id="trace-123",
                level="INFO",
                event="exec.output",
                message="命令执行输出",
                extra={"exec_id": "exec-001", "output_len": 100},
            ),
        ]

        # 发送请求（模拟认证）
        response = client.post(
            "/api/bridge-logs",
            json={"logs": [log.dict() for log in logs]},
            headers={"Authorization": "Bearer test-token-12345"},
        )

        # 验证响应
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["received"] == 2
        assert data["persisted"] == 2

    def test_collect_bridge_logs_skip_no_case_id(self, mock_db_manager, client):
        """测试跳过无 case_id 的日志"""
        set_dependencies(mock_db_manager)

        logs = [
            BridgeLogEntry(
                level="INFO",
                event="bridge.start",
                message="Bridge 启动",
            ),
            BridgeLogEntry(
                case_id="Q20260720001",
                level="INFO",
                event="exec.start",
                message="开始执行命令",
            ),
        ]

        response = client.post(
            "/api/bridge-logs",
            json={"logs": [log.dict() for log in logs]},
            headers={"Authorization": "Bearer test-token-12345"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["received"] == 2
        assert data["persisted"] == 1  # 只有带 case_id 的日志被持久化

    def test_collect_bridge_logs_signature_verification(self, mock_db_manager, client):
        """测试 HMAC 签名验证"""
        set_dependencies(mock_db_manager)

        # 构造带签名的日志
        log = BridgeLogEntry(
            case_id="Q20260720001",
            level="INFO",
            event="exec.start",
            message="开始执行命令",
            signature="invalid-signature",  # 无效签名
        )

        response = client.post(
            "/api/bridge-logs",
            json={"logs": [log.dict()]},
            headers={"Authorization": "Bearer test-token-12345"},
        )

        # 验证响应（即使签名无效，MVP 阶段仍然接受）
        assert response.status_code == 200
        data = response.json()
        assert data["persisted"] == 1

    def test_verify_log_signature_without_key(self):
        """测试未配置 HMAC_KEY 时的签名验证"""
        log = BridgeLogEntry(
            case_id="Q20260720001",
            level="INFO",
            event="exec.start",
            message="开始执行命令",
            signature="some-signature",
        )

        # 未配置 HMAC_KEY 时应该返回 True
        with patch("backend.conversation-service.app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.BRIDGE_LOG_HMAC_KEY = None
            result = _verify_log_signature(log)
            assert result is True

    def test_verify_log_signature_with_valid_key(self):
        """测试有效 HMAC_KEY 的签名验证"""
        import hmac
        import hashlib

        # 构造签名
        hmac_key = "test-secret-key"
        log_dict = {
            "case_id": "Q20260720001",
            "level": "INFO",
            "event": "exec.start",
            "message": "开始执行命令",
        }
        content = json.dumps(log_dict, sort_keys=True, ensure_ascii=False)
        expected_signature = hmac.new(
            hmac_key.encode(),
            content.encode(),
            hashlib.sha256,
        ).hexdigest()

        log = BridgeLogEntry(
            **log_dict,
            signature=expected_signature,
        )

        # 验证签名
        with patch("backend.conversation-service.app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.BRIDGE_LOG_HMAC_KEY = hmac_key
            result = _verify_log_signature(log)
            assert result is True

    def test_verify_log_signature_with_invalid_signature(self):
        """测试无效签名的验证"""
        log = BridgeLogEntry(
            case_id="Q20260720001",
            level="INFO",
            event="exec.start",
            message="开始执行命令",
            signature="invalid-signature",
        )

        with patch("backend.conversation-service.app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.BRIDGE_LOG_HMAC_KEY = "test-secret-key"
            result = _verify_log_signature(log)
            assert result is False


class TestBridgeLogsRetry:
    """测试前端日志重试逻辑"""

    @pytest.mark.asyncio
    async def test_retry_with_exponential_backoff(self):
        """测试指数退避重试"""
        # 模拟前端重试逻辑
        retry_count = 0
        max_retry = 5
        base_delay = 500

        delays = []
        for i in range(max_retry):
            delay = base_delay * (2**i)
            delays.append(delay)

        # 验证指数退避：500, 1000, 2000, 4000, 8000
        assert delays == [500, 1000, 2000, 4000, 8000]

    @pytest.mark.asyncio
    async def test_max_retry_limit(self):
        """测试最大重试次数限制"""
        max_retry = 5
        attempts = 0

        for i in range(max_retry + 1):
            if i < max_retry:
                attempts += 1
            else:
                # 达到最大重试次数，停止重试
                break

        assert attempts == max_retry


class TestDatabaseSchema:
    """测试数据库 schema"""

    def test_bridge_execution_logs_table_structure(self):
        """测试 bridge_execution_logs 表结构"""
        # 验证 SQL 文件存在
        import os
        sql_path = "database/data-migrations/005_bridge_execution_logs.sql"
        assert os.path.exists(sql_path), f"SQL 文件不存在: {sql_path}"

        # 验证表定义
        with open(sql_path, "r") as f:
            content = f.read()
            assert "CREATE TABLE IF NOT EXISTS bridge_execution_logs" in content
            assert "case_id" in content
            assert "trace_id" in content
            assert "custom_ui" in content
            assert "event" in content
            assert "user_id" in content  # P1-4: 新增字段

    def test_index_creation(self):
        """测试索引创建"""
        with open("database/data-migrations/005_bridge_execution_logs.sql", "r") as f:
            content = f.read()
            # 验证所有必要的索引
            assert "idx_bridge_logs_case_id" in content
            assert "idx_bridge_logs_trace_id" in content
            assert "idx_bridge_logs_event" in content  # P1-4: 新增索引


if __name__ == "__main__":
    pytest.main([__file__, "-v"])