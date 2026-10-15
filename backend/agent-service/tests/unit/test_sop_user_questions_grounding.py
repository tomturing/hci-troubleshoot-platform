"""
S2/S3 假设生成 user_questions 事实源约束测试（工单 Q2026101511258「硬盘坏道」复盘）。

守护的失败模式：用户已通过 SOP 变量弹框提交 disk_dev 后，ReasoningOutput.user_questions
仍续写出「请提交 disk_dev」并对 SOP variable_schema 未声明的 disk_sn/disk_type 即兴索要。
filter_ungrounded_user_questions 以变量池为权威事实源，模型无关地剔除这两类幻觉追问。
"""

from __future__ import annotations

from app.adapters.agents.htp.react_engine import filter_ungrounded_user_questions

# 硬盘坏道 SOP#4 实际声明的变量集（无 disk_sn/disk_type）
DECLARED = {"alert_logs", "node_ip", "asan_disks", "disk_dev", "smart_info", "check_meth", "date"}
# 用户已提交 disk_dev（其余 asan_disks 采集失败无值，视为未就绪）
RESOLVED = {"node_ip", "disk_dev"}


def test_drops_reask_of_already_resolved_variable():
    """重复索取已就绪的 disk_dev → 判为冗余追问，丢弃。"""
    questions = ["请在 SOP 变量确认框中正式提交 disk_dev 的值（建议填写完整路径，如 /dev/sde）"]
    kept, dropped = filter_ungrounded_user_questions(questions, RESOLVED, DECLARED)
    assert kept == []
    assert dropped == questions


def test_drops_invented_undeclared_variable():
    """凭空索要 SOP 未声明的 disk_sn/disk_type → 判为幻觉需求项，丢弃。"""
    questions = ["请告知该查询返回的 disk_sn 与 disk_type，以便交叉校验"]
    kept, dropped = filter_ungrounded_user_questions(questions, RESOLVED, DECLARED)
    assert kept == []
    assert len(dropped) == 1


def test_keeps_question_for_declared_but_unresolved_variable():
    """引用已声明但未就绪的变量（smart_info）→ 合法补充请求，保留。"""
    questions = ["请提供 smart_info 的采集授权"]
    kept, dropped = filter_ungrounded_user_questions(questions, RESOLVED, DECLARED)
    assert kept == questions
    assert dropped == []


def test_keeps_question_without_variable_token():
    """不含变量样 token 的普通澄清问题 → 不误伤，保留。"""
    questions = ["请问该主机近期是否做过重启或硬件更换？"]
    kept, dropped = filter_ungrounded_user_questions(questions, RESOLVED, DECLARED)
    assert kept == questions
    assert dropped == []


def test_full_ticket_scenario_filters_only_hallucinated_questions():
    """还原 Q2026101511258 三条 user_questions，只剔除幻觉项、保留合法项。"""
    questions = [
        "请在 SOP 变量确认框中正式提交 disk_dev 的值（当前该变量槽位仍为 needs_input 状态）",  # 冗余（disk_dev 已就绪）：drop
        "您提供的 /sda 是否来自实际查询？若是请告知 disk_sn 与 disk_type",  # 未声明：drop
        "是否授权我重新采集 asan_disks 以定位 error 记录归属？",  # asan_disks 已声明未就绪：keep
    ]
    kept, dropped = filter_ungrounded_user_questions(questions, RESOLVED, DECLARED)
    assert kept == [questions[2]]
    assert set(dropped) == {questions[0], questions[1]}


def test_empty_input_returns_empty_lists():
    kept, dropped = filter_ungrounded_user_questions([], RESOLVED, DECLARED)
    assert kept == [] and dropped == []


def test_no_variable_context_keeps_everything():
    """拿不到 declared 声明集（如 variable_schema 加载失败）→ 无事实源护栏，幂等空操作，绝不误删。"""
    questions = ["请提供 disk_dev 的值", "请告知 disk_sn"]
    kept, dropped = filter_ungrounded_user_questions(questions, set(), set())
    assert kept == questions and dropped == []
