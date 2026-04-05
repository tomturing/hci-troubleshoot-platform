"""
S6 三选项流程单元测试 (v6.3)

测试 ConversationManager 中的两个新方法：
- build_pending_resolution(): 构造等待快照
- handle_resolution_choice(): 处理 A/B/C 选择

测试覆盖：
1. build_pending_resolution 快照格式正确性
2. handle_resolution_choice A/B/C 三条路径的返回值
3. 无效 choice 的异常处理
4. 4 条服务层约束的验证（通过 ConversationManager 纯函数部分）

注意：ConversationManager 是无状态纯函数，所有测试无需数据库。
"""

import re
from datetime import UTC, datetime

import pytest
from app.services.conversation_manager import ConversationManager


@pytest.fixture
def manager() -> ConversationManager:
    """创建无状态 ConversationManager 实例"""
    return ConversationManager()


# ─── build_pending_resolution 测试 ────────────────────────────────────────────


class TestBuildPendingResolution:
    """测试 build_pending_resolution() 快照格式"""

    def test_returns_dict_with_required_keys(self, manager: ConversationManager):
        """快照必须包含 stage、sent_at、options 三个字段"""
        result = manager.build_pending_resolution()
        assert isinstance(result, dict)
        assert "stage" in result
        assert "sent_at" in result
        assert "options" in result

    def test_stage_is_s6(self, manager: ConversationManager):
        """stage 字段固定为 'S6'"""
        result = manager.build_pending_resolution()
        assert result["stage"] == "S6"

    def test_options_are_abc(self, manager: ConversationManager):
        """options 包含且仅包含 A/B/C"""
        result = manager.build_pending_resolution()
        assert result["options"] == ["A", "B", "C"]

    def test_sent_at_is_valid_iso8601(self, manager: ConversationManager):
        """sent_at 是合法的 ISO 8601 字符串，且是近期时间"""
        result = manager.build_pending_resolution()
        sent_at_str = result["sent_at"]
        # 可解析为 datetime
        dt = datetime.fromisoformat(sent_at_str)
        # 时间是近期（5 秒内）
        diff = (datetime.now(UTC) - dt).total_seconds()
        assert 0 <= diff < 5, f"sent_at 时间偏差过大：{diff}s"

    def test_multiple_calls_have_different_sent_at(self, manager: ConversationManager):
        """多次调用产生不同的时间戳（幂等性测试）"""
        import time
        r1 = manager.build_pending_resolution()
        time.sleep(0.01)
        r2 = manager.build_pending_resolution()
        # sent_at 应该不同（时间流逝）
        assert r1["sent_at"] != r2["sent_at"]


# ─── handle_resolution_choice 测试 ───────────────────────────────────────────


class TestHandleResolutionChoice:
    """测试 handle_resolution_choice() 的三条路径"""

    # --- A 选项：已解决 ---

    def test_choice_a_action_is_resolve(self, manager: ConversationManager):
        """A 选项：action 为 'resolve'"""
        result = manager.handle_resolution_choice("A")
        assert result["action"] == "resolve"

    def test_choice_a_case_status_is_resolved(self, manager: ConversationManager):
        """A 选项：case.status 应更新为 'resolved'"""
        result = manager.handle_resolution_choice("A")
        assert result["case_status"] == "resolved"

    def test_choice_a_no_close_reason(self, manager: ConversationManager):
        """A 选项：close_reason 为 None（正常解决，不需要关闭原因）"""
        result = manager.handle_resolution_choice("A")
        assert result["close_reason"] is None

    def test_choice_a_stage_unchanged(self, manager: ConversationManager):
        """A 选项：不需要改变 diagnostic_stage"""
        result = manager.handle_resolution_choice("A")
        assert result["new_stage"] is None

    def test_choice_a_no_archive(self, manager: ConversationManager):
        """A 选项：不需要归档 diagnostic_item（问题已解决，保留历史）"""
        result = manager.handle_resolution_choice("A")
        assert result["archive_diagnostic_items"] is False

    def test_choice_a_clears_pending_resolution(self, manager: ConversationManager):
        """A 选项：清空 pending_resolution"""
        result = manager.handle_resolution_choice("A")
        assert result["clear_pending_resolution"] is True

    # --- B 选项：未解决回退 ---

    def test_choice_b_action_is_retry_s1(self, manager: ConversationManager):
        """B 选项：action 为 'retry_s1'"""
        result = manager.handle_resolution_choice("B")
        assert result["action"] == "retry_s1"

    def test_choice_b_case_status_unchanged(self, manager: ConversationManager):
        """B 选项：case.status 不变（仍为 confirmed）"""
        result = manager.handle_resolution_choice("B")
        assert result["case_status"] is None

    def test_choice_b_stage_back_to_s1(self, manager: ConversationManager):
        """B 选项：diagnostic_stage 回退到 S1"""
        result = manager.handle_resolution_choice("B")
        assert result["new_stage"] == "S1"

    def test_choice_b_archives_diagnostic_items(self, manager: ConversationManager):
        """B 选项（约束 4）：必须归档旧 diagnostic_item"""
        result = manager.handle_resolution_choice("B")
        assert result["archive_diagnostic_items"] is True

    def test_choice_b_no_close_reason(self, manager: ConversationManager):
        """B 选项：close_reason 为 None"""
        result = manager.handle_resolution_choice("B")
        assert result["close_reason"] is None

    def test_choice_b_clears_pending_resolution(self, manager: ConversationManager):
        """B 选项：清空 pending_resolution"""
        result = manager.handle_resolution_choice("B")
        assert result["clear_pending_resolution"] is True

    # --- C 选项：升级人工 ---

    def test_choice_c_action_is_escalate(self, manager: ConversationManager):
        """C 选项：action 为 'escalate'"""
        result = manager.handle_resolution_choice("C")
        assert result["action"] == "escalate"

    def test_choice_c_case_status_is_in_progress(self, manager: ConversationManager):
        """C 选项：case.status 更新为 'in_progress'"""
        result = manager.handle_resolution_choice("C")
        assert result["case_status"] == "in_progress"

    def test_choice_c_close_reason_is_escalated(self, manager: ConversationManager):
        """C 选项：close_reason 为 'escalated'（值域在 schema v6.3 新增）"""
        result = manager.handle_resolution_choice("C")
        assert result["close_reason"] == "escalated"

    def test_choice_c_stage_unchanged(self, manager: ConversationManager):
        """C 选项：不改变 diagnostic_stage（AI 退出后 stage 保持 S6）"""
        result = manager.handle_resolution_choice("C")
        assert result["new_stage"] is None

    def test_choice_c_no_archive(self, manager: ConversationManager):
        """C 选项：升级人工不归档 diagnostic_item（保留为人工查阅）"""
        result = manager.handle_resolution_choice("C")
        assert result["archive_diagnostic_items"] is False

    def test_choice_c_clears_pending_resolution(self, manager: ConversationManager):
        """C 选项：清空 pending_resolution"""
        result = manager.handle_resolution_choice("C")
        assert result["clear_pending_resolution"] is True

    # --- 异常处理 ---

    def test_invalid_choice_raises_value_error(self, manager: ConversationManager):
        """无效选择应抛出 ValueError"""
        with pytest.raises(ValueError, match="无效的 S6 选择"):
            manager.handle_resolution_choice("D")  # type: ignore[arg-type]

    def test_lowercase_choice_raises_value_error(self, manager: ConversationManager):
        """小写 a/b/c 不被接受（避免歧义）"""
        with pytest.raises(ValueError, match="无效的 S6 选择"):
            manager.handle_resolution_choice("a")  # type: ignore[arg-type]

    def test_empty_choice_raises_value_error(self, manager: ConversationManager):
        """空字符串应抛出 ValueError"""
        with pytest.raises(ValueError, match="无效的 S6 选择"):
            manager.handle_resolution_choice("")  # type: ignore[arg-type]


# ─── 约束验证（通过 ConversationManager 行为验证） ─────────────────────────────


class TestConstraints:
    """验证 4 条服务层约束的 ConversationManager 行为部分"""

    def test_constraint4_choice_b_always_archives(self, manager: ConversationManager):
        """
        约束 4：B 选项必须触发 archive_diagnostic_items=True。

        这保证服务层在执行 DB 操作时一定会先批量归档旧记录，
        再改变 diagnostic_stage，不会出现只改 stage 而遗留旧记录的情况。
        """
        b_result = manager.handle_resolution_choice("B")
        assert b_result["archive_diagnostic_items"] is True
        # A 和 C 不归档
        a_result = manager.handle_resolution_choice("A")
        c_result = manager.handle_resolution_choice("C")
        assert a_result["archive_diagnostic_items"] is False
        assert c_result["archive_diagnostic_items"] is False

    def test_all_choices_clear_pending_resolution(self, manager: ConversationManager):
        """所有选择（A/B/C）都必须清空 pending_resolution（等待状态结束）"""
        for choice in ("A", "B", "C"):
            result = manager.handle_resolution_choice(choice)
            assert result["clear_pending_resolution"] is True, (
                f"选 {choice} 应清空 pending_resolution，但返回 {result['clear_pending_resolution']}"
            )

    def test_only_b_changes_stage(self, manager: ConversationManager):
        """只有 B 选项会改变 diagnostic_stage（回退 S1），A 和 C 不改变阶段"""
        assert manager.handle_resolution_choice("A")["new_stage"] is None
        assert manager.handle_resolution_choice("B")["new_stage"] == "S1"
        assert manager.handle_resolution_choice("C")["new_stage"] is None

    def test_pending_resolution_snapshot_is_immutable_per_call(
        self, manager: ConversationManager
    ):
        """
        每次调用 build_pending_resolution 都返回新快照（不共享引用），
        避免调用方修改快照后影响其他地方。
        """
        r1 = manager.build_pending_resolution()
        r2 = manager.build_pending_resolution()
        # 修改 r1 不影响 r2
        r1["options"].append("X")
        assert "X" not in r2["options"]


# ─── 与原有状态转换逻辑的回归测试 ─────────────────────────────────────────────


class TestS6BackwardCompatibility:
    """
    回归测试：新增 S6 方法不破坏原有状态转换逻辑。

    重点验证 S6 是 detect_stage_transition 的终态，
    没有任何正则触发词会让 S6 再推进。
    """

    def test_s6_is_terminal_for_detect_transition(self, manager: ConversationManager):
        """S6 已是最后阶段，detect_stage_transition 返回 None（不推进）"""
        reply = "根因确认：问题已解决，系统已恢复正常。"
        result = manager.detect_stage_transition("S6", reply, "")
        assert result is None

    def test_s5_to_s6_transition_still_works(self, manager: ConversationManager):
        """S5→S6 的原有触发词不受影响"""
        reply = "请按上述步骤操作，执行以上步骤后请告知我是否成功解决问题。"
        result = manager.detect_stage_transition("S5", reply, "")
        assert result == "S6"

    def test_new_methods_dont_affect_extract_category(
        self, manager: ConversationManager
    ):
        """新增方法不影响 extract_category 的正常工作"""
        reply = "已确认故障分类：虚拟机-003 虚拟机开机失败"
        result = manager.extract_category(reply)
        assert result is not None
        assert result["code"] == "虚拟机-003"
        assert result["name"] == "虚拟机开机失败"
