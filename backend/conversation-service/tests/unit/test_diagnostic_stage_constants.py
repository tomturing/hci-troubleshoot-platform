"""
诊断阶段常量测试（T-AGT-13：语义命名重构）

验证 DiagnosticStage 常量和 STAGE_LABELS 映射
"""


class TestDiagnosticStageConstants:
    """诊断阶段常量测试"""

    def test_all_stages_defined(self):
        """验证所有诊断阶段常量已定义"""
        from app.models.diagnostic_state import DiagnosticStage

        stages = [
            DiagnosticStage.TRIAGE,
            DiagnosticStage.INVESTIGATION,
            DiagnosticStage.S1_LOCATION,
            DiagnosticStage.S2_HYPOTHESIS,
            DiagnosticStage.S3_VERIFICATION,
            DiagnosticStage.S4_ROOT_CAUSE,
            DiagnosticStage.REMEDIATION,
            DiagnosticStage.CLOSURE,
            DiagnosticStage.TRIAGE_FAILED,
        ]

        for stage in stages:
            assert isinstance(stage, str)
            assert len(stage) > 0

    def test_stage_values(self):
        """验证阶段常量值正确（保持数据库兼容）"""
        from app.models.diagnostic_state import DiagnosticStage

        assert DiagnosticStage.TRIAGE == "S0"
        assert DiagnosticStage.INVESTIGATION == "S1"
        assert DiagnosticStage.S1_LOCATION == "S1"
        assert DiagnosticStage.S2_HYPOTHESIS == "S2"
        assert DiagnosticStage.S3_VERIFICATION == "S3"
        assert DiagnosticStage.S4_ROOT_CAUSE == "S4"
        assert DiagnosticStage.REMEDIATION == "S5"
        assert DiagnosticStage.CLOSURE == "S6"
        assert DiagnosticStage.TRIAGE_FAILED == "S0_FAILED"

    def test_legacy_aliases_compatible(self):
        """验证旧命名别名兼容"""
        from app.models.diagnostic_state import DiagnosticStage

        # 旧命名值与新命名相同（数据库兼容）
        assert DiagnosticStage.S0_INTENT == DiagnosticStage.TRIAGE
        assert DiagnosticStage.S5_SOLUTION == DiagnosticStage.REMEDIATION
        assert DiagnosticStage.S0_FAILED == DiagnosticStage.TRIAGE_FAILED

    def test_stage_labels_complete(self):
        """验证所有阶段都有对应的标签"""
        from app.models.diagnostic_state import STAGE_LABELS, DiagnosticStage

        expected_stages = [
            DiagnosticStage.TRIAGE,
            DiagnosticStage.INVESTIGATION,
            DiagnosticStage.S1_LOCATION,
            DiagnosticStage.S2_HYPOTHESIS,
            DiagnosticStage.S3_VERIFICATION,
            DiagnosticStage.S4_ROOT_CAUSE,
            DiagnosticStage.REMEDIATION,
            DiagnosticStage.CLOSURE,
            DiagnosticStage.TRIAGE_FAILED,
        ]

        for stage in expected_stages:
            assert stage in STAGE_LABELS, f"阶段 {stage} 缺少标签定义"
            assert isinstance(STAGE_LABELS[stage], str)
            assert len(STAGE_LABELS[stage]) > 0

    def test_stage_groups_defined(self):
        """验证阶段分组正确"""
        from app.models.diagnostic_state import DiagnosticStage

        assert "S0" in DiagnosticStage.TRIAGE_STAGES
        assert "S0_FAILED" in DiagnosticStage.TRIAGE_STAGES
        assert "S1" in DiagnosticStage.INVESTIGATION_STAGES
        assert "S2" in DiagnosticStage.INVESTIGATION_STAGES
        assert "S3" in DiagnosticStage.INVESTIGATION_STAGES
        assert "S4" in DiagnosticStage.INVESTIGATION_STAGES
        assert "S5" in DiagnosticStage.REMEDIATION_STAGES
        assert "S6" in DiagnosticStage.CLOSURE_STAGES

    def test_triage_failed_stage_constant(self):
        """验证 TRIAGE_FAILED 常量定义正确"""
        from app.models.diagnostic_state import DiagnosticStage

        assert DiagnosticStage.TRIAGE_FAILED == "S0_FAILED"
        assert DiagnosticStage.TRIAGE_FAILED != DiagnosticStage.TRIAGE

    def test_stage_labels_format(self):
        """验证阶段标签格式正确"""
        from app.models.diagnostic_state import STAGE_LABELS

        for stage, label in STAGE_LABELS.items():
            # 标签格式应为 "语义名-中文名称" 或 "SX-中文名称"
            assert "-" in label, f"阶段 {stage} 的标签格式不正确: {label}"
            parts = label.split("-", 1)
            assert len(parts) == 2
