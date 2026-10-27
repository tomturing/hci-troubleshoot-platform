"""check_docs_frontmatter 的回归测试。

防护目标：**每一条 §7.3 契约都必须真的能拦住违规**。

为什么单独立这组测试：这个门禁的全部价值在于「让按 status/category/audience
过滤时不静默漏文档」。如果它只对合规样本返回空、对违规样本也返回空，
那它就是一条永远通过的空转门禁——比没有门禁更糟（给了虚假的安全感）。
所以每条契约都配一个「必须报错」的用例，另有若干「不得误报」的用例。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "check_docs_frontmatter.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_frontmatter", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(**overrides: str) -> str:
    """生成一份带 frontmatter 的文档正文，overrides 覆盖默认字段。"""
    fields = {
        "status": "active",
        "category": "solution",
        "audience": "developer",
        "last_updated": "2026-10-26",
        "owner": "team",
    }
    fields.update(overrides)
    head = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"---\n{head}\n---\n\n# 标题\n\n正文。\n"


# ─────────────────────── 合规样本：不得误报 ───────────────────────


def test_compliant_doc_passes():
    module = _load_module()
    assert module.check_frontmatter("solution/agent/agent设计.md", _doc()) == []


def test_template_inline_comments_are_stripped():
    """规范 §7.3 模板本身就带行内注释，照抄模板不能被判违规。"""
    module = _load_module()
    text = (
        "---\n"
        "status: active          # active / archived（见 §7.3.1）\n"
        "category: solution      # requirement / solution / task / deploy / verify / meta\n"
        "audience: developer     # 受控词表，逗号分隔多值（见 §7.3.2）\n"
        "last_updated: 2026-10-26\n"
        "owner: team\n"
        "---\n\n# 标题\n"
    )
    assert module.check_frontmatter("solution/agent/agent设计.md", text) == []


def test_multi_value_audience_passes():
    """§7.3.2 允许多值，逗号+空格即可。"""
    module = _load_module()
    assert module.check_frontmatter("solution/agent/agent设计.md", _doc(audience="developer, qa")) == []


def test_events_and_archive_are_exempt():
    """事件文档遵循 §8、归档件冻结，都不受 §7.3 约束。"""
    module = _load_module()
    assert module.is_current_doc("solution/events/2026-04-05-X方案.md") is False
    assert module.is_current_doc("task/events/2026-04-05-X任务.md") is False
    assert module.is_current_doc("archive/agent-external-2026-10/README.md") is False
    assert module.is_current_doc("solution/agent/agent设计.md") is True


# ─────────────────────── 违规样本：必须拦住 ───────────────────────


def test_missing_frontmatter_is_blocking():
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", "# 标题\n\n正文。\n")

    assert findings
    assert "缺少 frontmatter" in findings[0]


def test_horizontal_rule_in_body_is_not_frontmatter():
    """正文里的 --- 是水平分割线：只有文件最开头的那一块算元数据。"""
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", "# 标题\n\n---\n\n正文。\n")

    assert findings
    assert "缺少 frontmatter" in findings[0]


def test_missing_required_field_is_blocking():
    module = _load_module()
    text = _doc()
    text = text.replace("owner: team\n", "")

    findings = module.check_frontmatter("solution/agent/agent设计.md", text)

    assert findings
    assert "缺少必填字段：owner" in findings[0]


def test_empty_required_field_is_blocking():
    """写了 key 但值为空，与没写等价——过滤时同样是坏数据。"""
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(owner=""))

    assert findings
    assert "owner" in findings[0]


def test_functional_status_is_blocking():
    """§7.3.1：implemented 不是文档生命周期状态，必须拦住。"""
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(status="implemented"))

    assert findings
    assert "status='implemented' 非法" in findings[0]
    assert "§7.3.1" in findings[0]


def test_invalid_category_is_blocking():
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(category="architecture"))

    assert findings
    assert "category='architecture' 非法" in findings[0]


def test_unknown_audience_role_is_blocking():
    """历史遗留的 engineer 必须报错（规范要求一律视为 developer），不能静默放过。"""
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(audience="engineer"))

    assert findings
    assert "词表外角色：engineer" in findings[0]


def test_all_must_be_exclusive():
    """all 与具体角色并列是自相矛盾。"""
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(audience="all, developer"))

    assert findings
    assert "独占" in findings[0]


def test_audience_separator_must_be_comma_space():
    """§7.3.2 规则 1：不写 developer,qa，也不写 [developer, qa]。"""
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(audience="developer,qa"))

    assert findings
    assert "分隔符不规范" in findings[0]


def test_bad_last_updated_format_is_blocking():
    module = _load_module()
    findings = module.check_frontmatter("solution/agent/agent设计.md", _doc(last_updated="2026/10/26"))

    assert findings
    assert "last_updated='2026/10/26' 非法" in findings[0]


def test_multiple_violations_are_reported_together():
    """一篇文档多处违规时一次报全，避免「改一处、再跑一次、再报一处」的往返。"""
    module = _load_module()
    findings = module.check_frontmatter(
        "solution/agent/agent设计.md",
        _doc(status="proposed", category="其他", audience="engineer", last_updated="2026年10月"),
    )

    assert len(findings) == 1
    text = findings[0]
    for expected in ("status=", "category=", "词表外角色", "last_updated="):
        assert expected in text
