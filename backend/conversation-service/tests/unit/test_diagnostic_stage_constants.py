"""
诊断阶段常量测试

验证 DiagnosticStage 常量和 STAGE_LABELS 映射
"""



class TestDiagnosticStageConstants:
    """诊断阶段常量测试"""

    def test_all_stages_defined(self):
        """验证所有诊断阶段常量已定义"""
        from app.models.diagnostic_state import DiagnosticStage

        stages = [
            DiagnosticStage.S0_INTENT,
            DiagnosticStage.S1_LOCATION,
            DiagnosticStage.S2_HYPOTHESIS,
            DiagnosticStage.S3_VERIFICATION,
            DiagnosticStage.S4_ROOT_CAUSE,
            DiagnosticStage.S5_SOLUTION,
            DiagnosticStage.S6_CLOSURE,
            DiagnosticStage.S0_FAILED,
        ]

        for stage in stages:
            assert isinstance(stage, str)
            assert len(stage) > 0

    def test_stage_values(self):
        """验证阶段常量值正确"""
        from app.models.diagnostic_state import DiagnosticStage

        assert DiagnosticStage.S0_INTENT == "S0"
        assert DiagnosticStage.S1_LOCATION == "S1"
        assert DiagnosticStage.S2_HYPOTHESIS == "S2"
        assert DiagnosticStage.S3_VERIFICATION == "S3"
        assert DiagnosticStage.S4_ROOT_CAUSE == "S4"
        assert DiagnosticStage.S5_SOLUTION == "S5"
        assert DiagnosticStage.S6_CLOSURE == "S6"
        assert DiagnosticStage.S0_FAILED == "S0_FAILED"

    def test_stage_labels_complete(self):
        """验证所有阶段都有对应的标签"""
        from app.models.diagnostic_state import STAGE_LABELS, DiagnosticStage

        expected_stages = [
            DiagnosticStage.S0_INTENT,
            DiagnosticStage.S1_LOCATION,
            DiagnosticStage.S2_HYPOTHESIS,
            DiagnosticStage.S3_VERIFICATION,
            DiagnosticStage.S4_ROOT_CAUSE,
            DiagnosticStage.S5_SOLUTION,
            DiagnosticStage.S6_CLOSURE,
            DiagnosticStage.S0_FAILED,
        ]

        for stage in expected_stages:
            assert stage in STAGE_LABELS, f"阶段 {stage} 缺少标签定义"
            assert isinstance(STAGE_LABELS[stage], str)
            assert len(STAGE_LABELS[stage]) > 0

    def test_s0_failed_stage_constant(self):
        """验证 S0_FAILED 常量定义正确"""
        from app.models.diagnostic_state import DiagnosticStage

        assert DiagnosticStage.S0_FAILED == "S0_FAILED"
        assert DiagnosticStage.S0_FAILED != DiagnosticStage.S0_INTENT

    def test_stage_labels_format(self):
        """验证阶段标签格式正确"""
        from app.models.diagnostic_state import STAGE_LABELS

        for stage, label in STAGE_LABELS.items():
            # 标签格式应为 "SX-中文名称"
            assert "-" in label, f"阶段 {stage} 的标签格式不正确: {label}"
            parts = label.split("-", 1)
            assert len(parts) == 2
            assert parts[0].startswith("S"), f"阶段前缀应为 S 开头: {label}"
