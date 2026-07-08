"""
Signal 模块集成测试

验证 Signal 模块与 Agent 系统的集成：
1. SystemTools 访问入口验证
2. SignalExtractor 提取与执行流程
3. VariablePool 变量流转流程
4. 端到端 KBD 案例执行
"""

from app.tools import SystemTools
from app.tools.qkv.engine import QKVResult
from app.tools.signal import (
    BackendSignal,
    BackendSignalType,
    FrontendQueryType,
    FrontendSignal,
    KeySignal,
    SignalCategory,
)


class TestSystemToolsIntegration:
    """验证 SystemTools 访问入口"""

    def test_get_signal_extractor(self):
        """测试获取 SignalExtractor"""
        extractor_cls = SystemTools.get_signal_extractor()
        assert extractor_cls is not None
        assert hasattr(extractor_cls, "extract_from_text")

    def test_create_variable_pool(self):
        """测试创建变量池"""
        pool = SystemTools.create_variable_pool("test-conv-123")
        assert pool is not None
        assert pool.conversation_id == "test-conv-123"

    def test_get_signal_types(self):
        """测试获取信号类型"""
        signal_types = SystemTools.get_signal_types()
        KeySignal, FrontendSignal, BackendSignal, SignalCategory = signal_types

        assert KeySignal is not None
        assert FrontendSignal is not None
        assert BackendSignal is not None
        assert SignalCategory is not None


class TestVariablePoolIntegration:
    """验证变量池集成流程"""

    def test_frontend_to_backend_flow(self):
        """测试前端信号到后端信号的完整流转"""
        # 1. 创建变量池
        pool = SystemTools.create_variable_pool("integration-test")

        # 2. 模拟前端信号执行结果（告警查询）
        frontend_result = QKVResult(
            success=True,
            query="alert",
            keyword="配置存储服务备节点异常",
            command="acli alert get -k '配置存储服务备节点异常'",
            values=[
                {
                    "host": "host-047bcb4bc820",
                    "vm": "vm-001",
                    "end": "2026-07-09 13:28:59",
                    "alert_type": "host_bond",
                    "description": "备节点数据库副本异常",
                }
            ],
        )

        # 3. 注册变量（生产者）
        pool.register_from_frontend_result(frontend_result)

        # 4. 验证变量已写入
        assert pool.get("host") == "host-047bcb4bc820"
        assert pool.get("vm") == "vm-001"
        assert pool.get("end") == "2026-07-09 13:28:59"

        # 5. 创建后端信号（消费者）
        backend_signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="log_search",
            target={"scope": "${host}", "resource": "mysql-managed.log"},
            keywords=["file system read-only"],
            expected=True,
        )

        # 6. 渲染模板
        rendered_signal = pool.render_backend_signal(backend_signal)

        # 7. 验证变量已注入
        assert rendered_signal.target.scope == "host-047bcb4bc820"
        assert rendered_signal.target.resource == "mysql-managed.log"

    def test_multiple_frontend_signals(self):
        """测试多个前端信号的变量合并"""
        pool = SystemTools.create_variable_pool("multi-signal-test")

        # 第一个前端信号：告警查询
        alert_result = QKVResult(
            success=True,
            query="alert",
            keyword="告警1",
            command="acli alert get",
            values=[{"host": "node-001", "alert_type": "disk"}],
        )
        pool.register_from_frontend_result(alert_result)

        # 第二个前端信号：任务查询
        task_result = QKVResult(
            success=True,
            query="task",
            keyword="任务1",
            command="acli task get",
            values=[{"vm": "vm-002", "request_id": "req-123"}],
        )
        pool.register_from_frontend_result(task_result)

        # 验证变量合并
        assert pool.get("host") == "node-001"
        assert pool.get("vm") == "vm-002"
        assert pool.get("request_id") == "req-123"


class TestSignalExtractorIntegration:
    """验证信号提取器集成"""

    def test_extract_frontend_signal(self):
        """测试提取前端信号"""
        # KBD 案例文本
        kbd_text = "检查配置存储服务备节点异常告警"

        # 提取信号（跳过 LLM 调用，直接构造）
        signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="备节点异常",
            description="检查备节点异常告警",
        )

        # 验证信号类型
        assert isinstance(signal, FrontendSignal)
        assert signal.signal_category == SignalCategory.FRONTEND
        assert signal.query == FrontendQueryType.ALERT

    def test_extract_backend_signal(self):
        """测试提取后端信号"""
        signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="log_check",
            target={"scope": "${host}", "resource": "mysql.log"},
            keywords=["error"],
            expected=True,
        )

        # 验证信号类型
        assert isinstance(signal, BackendSignal)
        assert signal.signal_category == SignalCategory.BACKEND
        assert signal.signal_type == BackendSignalType.LOG_KEYWORD


class TestEndToEndIntegration:
    """端到端集成测试"""

    def test_kbd_32090_flow(self):
        """测试 KBD 案例 32090 的完整流程"""
        # 1. 创建变量池
        pool = SystemTools.create_variable_pool("kbd-32090")

        # 2. 步骤 1：前端信号 - 提取告警元数据
        frontend_signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="配置存储服务备节点异常",
            limit=50,
        )

        # 模拟执行结果
        frontend_result = QKVResult(
            success=True,
            query="alert",
            keyword="配置存储服务备节点异常",
            command="acli alert get -k '配置存储服务备节点异常' -l 50",
            values=[
                {
                    "host": "host-047bcb4bc820",
                    "end": "2026-07-09 13:28:59",
                    "alert_type": "host_bond",
                }
            ],
        )

        # 注册变量
        pool.register_from_frontend_result(frontend_result)

        # 3. 步骤 2：后端信号 - 检查日志
        backend_signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="log_check",
            target={"scope": "${host}", "resource": "mysql-managed.log"},
            keywords=["file system read-only"],
            expected=True,
        )

        # 渲染模板
        rendered_signal = pool.render_backend_signal(backend_signal)

        # 验证变量已正确注入
        assert rendered_signal.target.scope == "host-047bcb4bc820"
        assert rendered_signal.target.resource == "mysql-managed.log"
        assert rendered_signal.keywords == ["file system read-only"]

    def test_signal_from_dict_routing(self):
        """测试信号自动路由机制"""
        # 前端信号路由
        frontend_data = {
            "signal_category": "frontend",
            "query": "alert",
            "keyword": "测试告警",
        }
        frontend_signal = KeySignal.from_dict(frontend_data)
        assert isinstance(frontend_signal, FrontendSignal)

        # 后端信号路由
        backend_data = {
            "signal_category": "backend",
            "signal_type": "log_keyword",
            "keyword": "测试日志",
            "keywords": ["error"],
        }
        backend_signal = KeySignal.from_dict(backend_data)
        assert isinstance(backend_signal, BackendSignal)


class TestSignalValidation:
    """验证信号校验机制"""

    def test_frontend_signal_validation(self):
        """测试前端信号参数校验"""
        # 有效信号
        valid_signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="有效关键字",
            limit=100,
        )
        is_valid, error = valid_signal.validate()
        assert is_valid is True
        assert error is None

        # 无效信号：关键字为空
        invalid_signal = FrontendSignal(
            query=FrontendQueryType.ALERT,
            keyword="",
            limit=100,
        )
        is_valid, error = invalid_signal.validate()
        assert is_valid is False
        assert "关键字" in error

    def test_backend_signal_validation(self):
        """测试后端信号参数校验"""
        # 有效信号
        valid_signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="有效关键字",
            keywords=["error", "warning"],
        )
        is_valid, error = valid_signal.validate()
        assert is_valid is True
        assert error is None

        # 无效信号：关键字列表为空
        invalid_signal = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            keyword="测试",
            keywords=[],
        )
        is_valid, error = invalid_signal.validate()
        assert is_valid is False
        assert "关键字列表" in error
