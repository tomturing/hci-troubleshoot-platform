"""
关键信号基类架构单元测试

验证 KeySignal 继承体系、信号提取器与变量池管理器
"""

import os
import sys

# 注入工程后端路径以兼容测试规范
_backend = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
_agent_service = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service"))
if _backend not in sys.path:
    sys.path.insert(0, _backend)
if _agent_service not in sys.path:
    sys.path.insert(0, _agent_service)

import pytest
from app.tools.qkv.engine import QKVResult
from app.tools.signal import (
    BackendSignal,
    BackendSignalType,
    FrontendQueryType,
    FrontendSignal,
    KeySignal,
    SignalCategory,
    VariablePool,
)

# ─────────────────────────────────────────────────────────────────────────────
# KeySignal 基类测试
# ─────────────────────────────────────────────────────────────────────────────


class TestKeySignalBaseClass:
    """验证 KeySignal 基类功能"""

    def test_signal_category_enum(self):
        """测试信号类别枚举"""
        assert SignalCategory.FRONTEND.value == "frontend"
        assert SignalCategory.BACKEND.value == "backend"

    def test_cannot_instantiate_abstract_class(self):
        """验证 KeySignal 是抽象类，不能直接实例化"""
        with pytest.raises(TypeError):
            KeySignal(
                signal_category=SignalCategory.FRONTEND,
                keyword="test"
            )

    def test_from_dict_routes_to_frontend(self):
        """测试 from_dict 自动路由到 FrontendSignal"""
        data = {
            "signal_category": "frontend",
            "query": "alert",
            "keyword": "备节点异常",
        }
        signal = KeySignal.from_dict(data)
        assert isinstance(signal, FrontendSignal)
        assert signal.signal_category == SignalCategory.FRONTEND
        assert signal.query == FrontendQueryType.ALERT

    def test_from_dict_routes_to_backend(self):
        """测试 from_dict 自动路由到 BackendSignal"""
        data = {
            "signal_category": "backend",
            "signal_type": "log_keyword",
            "keyword": "test",
            "keywords": ["error"],
        }
        signal = KeySignal.from_dict(data)
        assert isinstance(signal, BackendSignal)
        assert signal.signal_category == SignalCategory.BACKEND
        assert signal.signal_type == BackendSignalType.LOG_KEYWORD


# ─────────────────────────────────────────────────────────────────────────────
# FrontendSignal 测试
# ─────────────────────────────────────────────────────────────────────────────


class TestFrontendSignal:
    """验证前端信号功能"""

    def test_create_alert_signal(self):
        """测试创建告警信号"""
        signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="配置存储服务备节点异常",
            limit=50
        )
        assert signal.signal_category == SignalCategory.FRONTEND
        assert signal.query == FrontendQueryType.ALERT
        assert signal.keyword == "配置存储服务备节点异常"
        assert signal.limit == 50

    def test_create_task_signal_with_is_failed(self):
        """测试创建任务信号（包含 is_failed 标志）"""
        signal = FrontendSignal(
            query=FrontendQueryType.TASK,
            keyword="启动虚拟机",
            is_failed=True,
            limit=10
        )
        assert signal.is_failed is True
        assert signal.limit == 10

    def test_extract_method(self):
        """测试 extract() 方法返回正确字典"""
        signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="test",
            description="测试信号"
        )
        extracted = signal.extract()
        assert extracted["signal_category"] == "frontend"
        assert extracted["query"] == "alert"
        assert extracted["keyword"] == "test"
        assert extracted["description"] == "测试信号"

    def test_validate_success(self):
        """测试参数校验成功"""
        signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="valid_keyword",
            limit=100
        )
        is_valid, error = signal.validate()
        assert is_valid is True
        assert error is None

    def test_validate_missing_keyword(self):
        """测试关键字缺失校验失败"""
        signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="",
            limit=100
        )
        is_valid, error = signal.validate()
        assert is_valid is False
        assert "关键字" in error

    def test_validate_invalid_limit(self):
        """测试 limit 超范围校验失败"""
        signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="test",
            limit=500  # 超过 200
        )
        is_valid, error = signal.validate()
        assert is_valid is False
        assert "limit" in error


# ─────────────────────────────────────────────────────────────────────────────
# BackendSignal 测试
# ─────────────────────────────────────────────────────────────────────────────


class TestBackendSignal:
    """验证后端信号功能"""

    def test_create_log_keyword_signal(self):
        """测试创建日志关键字信号"""
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="log_search",
            target={"scope": "{{HOST}}", "resource": "mysql.log"},
            keywords=["file system read-only"],
            expected=True
        )
        assert signal.signal_category == SignalCategory.BACKEND
        assert signal.signal_type == BackendSignalType.LOG_KEYWORD
        assert signal.target.scope == "{{HOST}}"
        assert signal.keywords == ["file system read-only"]

    def test_create_service_status_signal(self):
        """测试创建服务状态信号"""
        signal = BackendSignal(
            signal_type=BackendSignalType.SERVICE_STATUS,
            keyword="service_check",
            keywords=["running"],
            container="asv",
            expected=True
        )
        assert signal.signal_type == BackendSignalType.SERVICE_STATUS
        assert signal.container == "asv"

    def test_extract_method(self):
        """测试 extract() 方法"""
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="test",
            keywords=["error"],
            expected=False
        )
        extracted = signal.extract()
        assert extracted["signal_category"] == "backend"
        assert extracted["signal_type"] == "log_keyword"
        assert extracted["keywords"] == ["error"]
        assert extracted["expected"] is False

    def test_validate_success(self):
        """测试参数校验成功"""
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="test",
            keywords=["error", "warning"]
        )
        is_valid, error = signal.validate()
        assert is_valid is True

    def test_validate_missing_keywords(self):
        """测试关键字列表缺失校验失败"""
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="test",
            keywords=[]
        )
        is_valid, error = signal.validate()
        assert is_valid is False
        assert "关键字列表" in error

    def test_validate_invalid_match_mode(self):
        """测试无效匹配模式"""
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="test",
            keywords=["error"],
            match_mode="invalid_mode"
        )
        is_valid, error = signal.validate()
        assert is_valid is False
        assert "匹配模式" in error


# ─────────────────────────────────────────────────────────────────────────────
# VariablePool 测试
# ─────────────────────────────────────────────────────────────────────────────


class TestVariablePool:
    """验证变量池管理功能"""

    def test_register_and_get_variable(self):
        """测试变量注册与获取"""
        pool = VariablePool(conversation_id="test-conv-123")
        pool.register("host", "node-001")
        assert pool.get("host") == "node-001"

    def test_render_template_placeholder(self):
        """测试模板占位符渲染"""
        pool = VariablePool(conversation_id="test-conv")
        pool.register("HOST", "node-001")
        pool.register("END", "2026-07-09 10:00:00")

        # 纯占位符
        assert pool.render_template("{{HOST}}") == "node-001"
        assert pool.render_template("{{END}}") == "2026-07-09 10:00:00"

        # 混合文本
        assert pool.render_template("prefix-{{HOST}}-suffix") == "prefix-node-001-suffix"

        # 无占位符
        assert pool.render_template("plain-text") == "plain-text"

    def test_register_from_frontend_result(self):
        """测试从前端信号结果批量注册变量"""
        pool = VariablePool(conversation_id="test-conv")

        # 模拟 QKVResult
        result = QKVResult(
            success=True,
            query="alert",
            keyword="备节点异常",
            command="acli alert get",
            values=[
                {
                    "host": "node-001",
                    "vm": "vm-123",
                    "end": "2026-07-09 10:00:00",
                    "alert_type": "host_bond",
                }
            ]
        )

        pool.register_from_frontend_result(result)

        # 验证变量注册
        assert pool.get("HOST") == "node-001"
        assert pool.get("VM") == "vm-123"
        assert pool.get("END") == "2026-07-09 10:00:00"
        assert pool.get("ALERT_TYPE") == "host_bond"

    def test_render_backend_signal(self):
        """测试渲染后端信号的模板占位符"""
        pool = VariablePool(conversation_id="test-conv")
        pool.register("HOST", "node-001")

        # 原始信号（包含占位符）
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="test",
            target={"scope": "{{HOST}}", "resource": "mysql.log"},
            keywords=["error"]
        )

        # 渲染
        rendered_signal = pool.render_backend_signal(signal)

        # 验证渲染结果
        assert rendered_signal.target.scope == "node-001"
        assert rendered_signal.target.resource == "mysql.log"  # 无占位符，不变


# ─────────────────────────────────────────────────────────────────────────────
# 端到端流程测试
# ─────────────────────────────────────────────────────────────────────────────


class TestEndToEndFlow:
    """验证完整的生产者-消费者流程"""

    def test_producer_consumer_pattern(self):
        """测试完整的生产者-消费者模式"""
        # 1. 创建变量池
        pool = VariablePool(conversation_id="kbd-32090")

        # 2. 模拟前端信号执行结果
        frontend_result = QKVResult(
            success=True,
            query="alert",
            keyword="备节点异常",
            command="acli alert get -k '备节点异常'",
            values=[
                {
                    "host": "host-047bcb4bc820",
                    "end": "2026-06-09 13:28:59",
                    "alert_type": "host_bond",
                }
            ]
        )

        # 3. 注册变量（生产者）
        pool.register_from_frontend_result(frontend_result)

        # 4. 创建后端信号（消费者）
        backend_signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="log_check",
            target={"scope": "{{HOST}}", "resource": "mysql-managed.log"},
            keywords=["file system read-only"]
        )

        # 5. 渲染模板
        rendered_signal = pool.render_backend_signal(backend_signal)

        # 6. 验证变量流转
        assert rendered_signal.target.scope == "host-047bcb4bc820"
        assert rendered_signal.target.resource == "mysql-managed.log"
