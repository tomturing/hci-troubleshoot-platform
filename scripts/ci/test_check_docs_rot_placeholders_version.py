"""check_docs_rot 的 C4（占位符）/ C7（版本矛盾）判据回归测试。

Wave 6 把这两类从「全层裸匹配」重构为「现行层 + 语义上下文」判据，本文件把
重构后的射程钉死：**每一条放宽都必须配一条「不许放宽」的反向用例**，否则
豁免射程会在后续改动里悄悄扩大，门禁静默失效。

重构前后的实测数字（本机全仓扫描）：

- C4：172 条 → 0 条。172 条里现行层 57 条，绝大多数是标识符/示例值/业务
  术语（`V-xxx`、`sk-xxxx`、工单状态「待确认」）；真腐化只有 5 条（已修）。
- C7：5 个版本号被判「互相矛盾」 → 0 条。旧实现把 README 的历史发布说明
  （v2.1.1~v2.1.4、v2.23.0）当成互相矛盾的「当前版本声明」。

对抗性前提：C4/C7 只报告不阻断，但「只报告」不等于「可以恒报」。一个恒定
误报的检查器会训练评审忽略它，比没有检查器更糟（同 V-036 的失效模式）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "check_docs_rot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_rot", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rot():
    return _load_module()


# ─────────────────── 一、标记分级自洽性（防止静默失效） ───────────────────


def test_every_declared_marker_is_matched_by_candidate_regex(rot):
    """强/弱标记清单里的每一项都必须能被 PLACEHOLDER_RE 匹配到。

    旧实现的 `strong_markers` 里列了 未实现/尚未实现/未完成/尚未完成，但
    PLACEHOLDER_RE 根本没收录这四个词——清单与正则脱钩，标记永远不可能
    触发，且没有任何测试能发现。这条用例把两边钉在一起。
    """
    for marker in rot.C4_STRONG_MARKERS + rot.C4_WEAK_MARKERS:
        assert rot.PLACEHOLDER_RE.search(marker), f"标记 {marker} 不在候选正则里，永远不会触发"


def test_strong_and_weak_marker_tiers_are_disjoint(rot):
    """同一个词不能既算强标记又算弱标记，否则分级形同虚设。"""
    overlap = set(rot.C4_STRONG_MARKERS) & set(rot.C4_WEAK_MARKERS)
    assert not overlap, f"强弱分级重叠：{sorted(overlap)}"


# ─────────────────── 二、C4 正向：真腐化必须上报 ───────────────────


@pytest.mark.parametrize(
    "line",
    [
        "## 3. 数据流图（待补充）",
        "### 3.3 acli 命令分类（待补充）",
        "> **TODO**: 补充以下流程的 Mermaid 数据流图：",
        "| — | 待补充 | — | — |",
        "本节待完善，后续补充灰度策略",
        "该章节待更新，口径已变",
    ],
)
def test_unfinished_document_sections_are_reported(rot, line):
    """作者交代「自己这段还没写完」时必须上报——这是 C4 存在的唯一理由。"""
    assert rot.classify_placeholder_line(line), f"真腐化被漏报：{line}"


# ─────────────────── 三、C4 反向：四类正当语境必须不上报 ───────────────────


@pytest.mark.parametrize(
    "line",
    [
        "每一个 V-xxx / PIT-xxx 条目本身就是一条带变更记录的变更历史节",
        '> 1. 先在下方"PIT 编号注册表"分配编号（旧 PIT-xxx 保留）',
        "- 关闭工单后提示新建工单；系统生成唯一工单号 (QYYYYMMDDXXXXX)",
        "- `k3s-verify.sh` 第 3 节会自动创建并关闭测试工单（编号 Q20260310xxxxx）",
        "问题不是再增加几个 `revision_xxx` 字段，而是缺少领域边界",
        "`ghcrToken: ghp_xxxx` 是占位掩码",
    ],
)
def test_identifier_and_mask_patterns_are_not_reported(rot, line):
    """标识符/编号/掩码不是占位符：删掉它们会让读者看不懂格式说明。"""
    assert rot.classify_placeholder_line(line) == [], f"误报标识符：{line}"


@pytest.mark.parametrize(
    "line",
    [
        "2. **本地测试**：`psql -f database/data-migrations/xxx.sql`",
        "| 1 | 单元测试 | `pytest tests/xxx/` 100% pass |",
        "- `GET /api/v1/kbd/{id}/reanalyze-images/status?job_id=xxx`",
        "2. 示例只允许明显假值：`P@5sw0rd`、`<API_KEY>`、`sk-xxxx`。",
    ],
)
def test_inline_code_examples_are_not_reported(rot, line):
    """反引号内是字面示例，不是作者对自己的交代。"""
    assert rot.classify_placeholder_line(line) == [], f"误报行内代码示例：{line}"


@pytest.mark.parametrize(
    "line",
    [
        '若 `node_ip` 未能匹配，在"告警主机"行注明"待确认（匹配失败，原始 host 字段：xxx）"。',
        '| 跨 SOP 跳转 | 在当前 SOP 的 solution 中写"建议走 SOP: xxx" |',
        '{"message": "SOP 依赖了未注册或未启用的工具：xxx，请先创建它们。"}',
    ],
)
def test_quoted_literal_text_is_not_reported(rot, line):
    """引号包住的是产品文案/示例值，不是文档作者的未完成声明。"""
    assert rot.classify_placeholder_line(line) == [], f"误报引号内字面文本：{line}"


@pytest.mark.parametrize(
    "line",
    [
        "* **状态 (status)**：下拉选择框（待确认、已确认、处理中、已解决、已关闭、已取消）。",
        "`risk_level=2` 的工具会先向用户推送 SSE 事件等待确认，超时 120 秒后转人工。",
        "| P0 | qfk_log 路径规范化 | 待确认 | `..` 与越界根目录 Fail Closed |",
        "- **语义描述 / Inferences（模型推测）**：带「模型推断·待确认」标签的内容不能当作事实。",
    ],
)
def test_domain_status_terms_are_not_reported(rot, line):
    """「待确认」在本项目是工单状态/风险等级/模型推断标签，不是文档腐化。"""
    assert rot.classify_placeholder_line(line) == [], f"误报业务术语：{line}"


@pytest.mark.parametrize(
    "line",
    [
        "- **日志上传补采入口**：诊断未完成时 Custom-UI 提示上传本地日志。",
        "- `llm_inference` / `agent_pass` 在引擎中**尚未实现**，契约已禁止其作为自动策略。",
        "### 12.4 LLM 输出控制（未实现，独立迭代）",
        "- Capability 未实现。",
        "| `/cfs`、`/sf/cfg` | 配置，可能含凭据 | 未实现前 capability gap |",
    ],
)
def test_system_state_wording_is_not_reported(rot, line):
    """「未实现/未完成」几乎全在描述代码或流程状态，不是「文档没写完」。"""
    assert rot.classify_placeholder_line(line) == [], f"误报系统状态描述：{line}"


@pytest.mark.parametrize(
    "line",
    [
        "#### SOP 节点级 embedding（待实现）",
        "## 三、待实现的优化项",
        "### 2.4 KBD/工具的目标专家验证闭环（待确认演进）",
    ],
)
def test_roadmap_annotations_are_not_reported(rot, line):
    """功能路线图标注（待实现/待确认演进）是诚实现状，不是文档腐化。"""
    assert rot.classify_placeholder_line(line) == [], f"误报路线图标注：{line}"


def test_template_instruction_is_not_reported(rot):
    """发布记录表的「此处填写」是模板指令，删掉读者就不知道要填什么。"""
    assert rot.classify_placeholder_line("每次发布在此处填写，归入变更审计：") == []


def test_weak_marker_is_reported_when_real_strong_marker_coexists(rot):
    """弱标记降级不等于废弃：同行确有强标记时仍要上报（证明该分支没被写死）。"""
    hits = rot.classify_placeholder_line("TODO: 待确认灰度范围，本节待补充")
    assert "待确认" in hits, f"弱标记共现分支失效：{hits}"
    assert "待补充" in hits


def test_weak_marker_licensed_only_by_literal_todo_is_not_reported(rot):
    """反引号里的 `# TODO` 不能让同行的弱标记借光上报。

    这是实测到的唯一残留误报：一句「被 `# TODO` 注释保留为未完成状态」是
    引用他人未完成态作为证据，不是本文件没写完。
    """
    line = "> `route.py` 的 SOP 查询代码被 `# TODO` 注释保留为未完成状态，导致 SOP 体系虚设。"
    assert rot.classify_placeholder_line(line) == [], "反引号内的 TODO 给弱标记借光了"


# ─────────────────── 四、C4 扫描范围：史料不算腐化 ───────────────────


@pytest.mark.parametrize(
    ("rel_path", "in_scope"),
    [
        ("docs/solution/管理台设计.md", True),
        ("docs/deploy/pitfalls/k8s.md", True),
        ("README.md", True),
        ("docs/solution/events/2026-08-06-某方案.md", False),
        ("docs/deploy/events/2026-02-15-本地开发指南.md", False),
        ("docs/archive/kbd-signals-2026-09-23/知识库设计.md", False),
    ],
)
def test_placeholder_scope_is_current_layer_only(rot, rel_path, in_scope):
    """事件层/归档层是史料：当时写下的 TODO 是真实状态记录，不是腐化。"""
    assert rot.placeholder_in_scope(rel_path) is in_scope


def test_scan_skips_event_layer_but_catches_current_layer(rot, monkeypatch, tmp_path):
    """整条扫描链路上验一遍：同一条 TODO，放事件层不报、放现行层要报。"""
    monkeypatch.setattr(rot, "ROOT", tmp_path)
    event_doc = tmp_path / "docs" / "deploy" / "events" / "2026-01-01-x.md"
    current_doc = tmp_path / "docs" / "deploy" / "y.md"
    for path in (event_doc, current_doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## 3. 数据流图（待补充）\n", encoding="utf-8")

    assert rot.check_placeholders([event_doc]) == []
    issues = rot.check_placeholders([current_doc])
    assert len(issues) == 1
    assert issues[0]["type"] == "C4"
    assert issues[0]["line"] == 1


def test_fenced_code_block_is_not_scanned(rot, monkeypatch, tmp_path):
    """代码围栏里的 TODO 是示例代码。"""
    monkeypatch.setattr(rot, "ROOT", tmp_path)
    doc = tmp_path / "docs" / "z.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("```python\n# TODO: 这里待补充\n```\n", encoding="utf-8")
    assert rot.check_placeholders([doc]) == []


# ─────────────────── 五、C7 版本矛盾 ───────────────────


def _write_core(rot, monkeypatch, tmp_path, readme: str, version: str = "2.31.0"):
    """在当前 ROOT 下写最小化的 pyproject + README。"""
    monkeypatch.setattr(rot, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    return tmp_path


def test_stale_current_milestone_is_reported(rot, monkeypatch, tmp_path):
    """README 声称 v2.23.0 是「当前里程碑」而实际 2.31.0 —— 这是真漂移。"""
    _write_core(rot, monkeypatch, tmp_path, "### v2.23.0 当前里程碑（2026-08-06）\n")
    issues = rot.check_version_consistency()
    assert len(issues) == 1
    assert issues[0]["line"] == 1
    assert "2.23.0" in issues[0]["detail"] and "2.31.0" in issues[0]["detail"]


def test_current_version_before_token_is_reported(rot, monkeypatch, tmp_path):
    """关键词在前的写法同样要抓：「当前版本 v2.9.9」。"""
    _write_core(rot, monkeypatch, tmp_path, "> 当前版本 v2.9.9，详见 CHANGELOG\n")
    assert len(rot.check_version_consistency()) == 1


def test_history_release_notes_are_not_reported(rot, monkeypatch, tmp_path):
    """历史发布说明里的版本号是记录，不是「当前版本声明」。

    旧实现正是把这几行判成互相矛盾（含已弃用的 2.1.1~2.1.4），导致 C7 恒报。
    """
    readme = (
        "> 版本以 pyproject.toml 为唯一来源，请勿在本文件硬编码版本号。\n"
        "### v2.23.0 里程碑（2026-08-06）\n"
        "### v2.1.4 更新说明（2026-06-11）\n"
        "### v2.1.1 更新说明（2026-04-24）\n"
    )
    _write_core(rot, monkeypatch, tmp_path, readme)
    assert rot.check_version_consistency() == []


def test_matching_current_version_passes(rot, monkeypatch, tmp_path):
    """声明的当前版本与唯一来源一致时通过。"""
    _write_core(rot, monkeypatch, tmp_path, "> 当前版本 v2.31.0\n")
    assert rot.check_version_consistency() == []


def test_missing_package_version_is_reported_not_silently_passed(rot, monkeypatch, tmp_path):
    """唯一来源缺失时必须报出来，不能静默通过（fail-visible）。"""
    monkeypatch.setattr(rot, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    issues = rot.check_version_consistency()
    assert len(issues) == 1
    assert "无法判定" in issues[0]["detail"]


def test_doc_spec_version_namespace_is_excluded(rot, monkeypatch, tmp_path):
    """文档管理规范用自己的 v2.10 编号，与产品版本是两个命名空间，不得混比。"""
    _write_core(rot, monkeypatch, tmp_path, "> 当前版本 v2.31.0\n")
    spec = tmp_path / "docs" / "文档管理规范.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("> 当前版本 v2.10.0\n", encoding="utf-8")
    assert rot.check_version_consistency() == []
