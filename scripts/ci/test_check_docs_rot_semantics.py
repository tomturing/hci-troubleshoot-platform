"""check_docs_rot 引用语义与分级门禁的回归测试。

覆盖 Wave 3 修正的四类问题：

1. 目录链接：`[task/](task/)` 在 GitHub 上渲染为目录列表页，是可用信源。
   旧实现一律把 `x/` 改写为 `x/index.md` 再判存在性，导致 docs/README.md
   里 7 处目录导航全部误报。
2. 行内代码：反引号内的 `[文字](URL)` / `![截图](URL)` 是**字面文本**，常见
   于「示范禁止写法」的规则表。旧实现按链接解析，逼作者去改正确的句子。
3. 否定性/历史性陈述：文档明文记录某路径「不存在/已删除」时，该引用正是
   它要表达的事实。旧实现把 docs/solution/数据库设计.md 中「…均为不存在的
   路径」这句本身判为腐化。
4. 分级门禁：现行全量文档层（`docs/` 排除 `*/events/` 与 `docs/archive/`）已
   归零，开 `--current-blocking` 后该层失效引用不论增量一律阻断；事件层/归档
   层是史料，仍只按改动行判增量。判定逻辑必须可单测，不得内联在 main 里。

对抗性审查要求：每一条放宽都必须配一个「不许放宽」的反向用例，防止豁免
射程被扩大后门禁静默失效；豁免条目还必须显式携带理由，不得隐式降级。
分级同理：开了 `--current-blocking` 就必须能拦住现行层的存量，而没开时行为
必须与旧版逐字一致，否则新开关本身就是一个未被审查的变量。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "check_docs_rot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_rot", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """把模块的 ROOT 指向临时仓库，使路径校验以 tmp_path 为基准。"""
    module = _load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module, tmp_path


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def repo_with_git(tmp_path, monkeypatch):
    """gitignore 豁免需要真实的 .git 环境，额外提供一个已 init 的仓库 fixture。"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    module = _load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    # 路径判定结果缓存在模块全局，换仓库时必须清空，否则用例会互相污染
    module._IGNORE_CACHE.clear()
    return module, tmp_path


# ─────────────────────────── 1. 目录链接 ───────────────────────────


def test_dir_link_to_existing_dir_not_reported(repo):
    """目录链接指向真实存在的目录时是 GitHub 可用的导航页，不得报腐化。"""
    module, root = repo
    _write(root, "docs/task/a.md", "# A\n")
    doc = _write(root, "docs/README.md", "看 [task/](task/) 目录\n")
    assert module.check_internal_links([doc]) == []


def test_dir_link_to_missing_dir_reported(repo):
    """反向用例：目录链接指向不存在的目录，仍必须命中。"""
    module, root = repo
    _write(root, "docs/README.md", "看 [nope/](nope/) 目录\n")
    doc = root / "docs/README.md"
    issues = module.check_internal_links([doc])
    assert len(issues) == 1
    assert issues[0]["type"] == "C1"


def test_dir_link_with_index_md_still_passes(repo):
    """既有目录又有 index.md 时不得因新逻辑反而出错。"""
    module, root = repo
    _write(root, "docs/task/index.md", "# 索引\n")
    doc = _write(root, "docs/README.md", "看 [task/](task/)\n")
    assert module.check_internal_links([doc]) == []


def test_bad_link_without_trailing_slash_reported(repo):
    """新逻辑只作用于以 / 结尾的目录链接，普通文件链接判定不得放松。"""
    module, root = repo
    doc = _write(root, "docs/README.md", "看 [缺失](missing.md)\n")
    assert len(module.check_internal_links([doc])) == 1


# ─────────────────────────── 2. 行内代码 ───────────────────────────


def test_inline_code_syntax_example_not_reported(repo):
    """反引号内的 markdown 语法示例是字面文本，不得按链接解析。"""
    module, root = repo
    doc = _write(
        root,
        "docs/spec.md",
        "| 禁止保留图片占位符 | 正文中不得出现 `[IMAGE_N]` 或 `![截图](URL)` |\n",
    )
    assert module.check_internal_links([doc]) == []
    assert module.check_images([doc]) == []


def test_inline_code_link_literal_not_reported(repo):
    """反向用例：行内代码里的链接写法不得豁免行外的真坏链接。"""
    module, root = repo
    doc = _write(root, "docs/spec.md", '转换规则：`<a href="x">文字</a>` 写作 `[文字](URL)`\n')
    assert module.check_internal_links([doc]) == []


def test_real_bad_link_outside_inline_code_reported(repo):
    """豁免只覆盖反引号内部，行外的坏链接必须照常命中。"""
    module, root = repo
    doc = _write(root, "docs/spec.md", "示例 `[文字](URL)`，正式入口见 [指南](missing.md)\n")
    issues = module.check_internal_links([doc])
    assert len(issues) == 1
    assert "missing.md" in issues[0]["detail"]


def test_links_inside_code_fence_skipped(repo):
    """原有的围栏跳过能力不得因新逻辑退化。"""
    module, root = repo
    doc = _write(root, "docs/spec.md", "```bash\n[文字](missing.md)\n```\n")
    assert module.check_internal_links([doc]) == []


# ─────────────────────────── 3. 否定性/历史性陈述 ───────────────────────────


def test_negation_word_waives_with_reason(repo):
    module, root = repo
    doc = _write(
        root,
        "docs/solution/db.md",
        "本轮修正三处路径错误：`data-pipeline/kbd/ingest.py` 与 `scripts/sop/import.py` 均为不存在的路径。\n",
    )
    issues = module.check_repo_paths([doc])
    assert len(issues) == 2
    assert all(i.get("waived") for i in issues)
    # 理由必须显式，便于人工复核，不接受无声放宽
    assert all("不存在" in i["waive_reason"] for i in issues)


def test_negation_word_in_before_window_waives(repo):
    module, root = repo
    doc = _write(root, "docs/x.md", "原路径 `scripts/old.py` 已被重命名到别处。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1 and issues[0].get("waived")


def test_without_negation_word_reported(repo):
    """反向用例：邻近没有否定语义词时，坏路径必须照常阻断。"""
    module, root = repo
    doc = _write(root, "docs/x.md", "执行 `scripts/old.py` 完成初始化。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1
    assert not issues[0].get("waived")


def test_negation_word_outside_window_reported(repo):
    """把否定词写得足够远就不再是修饰关系，不得借此逃逸检查。"""
    module, root = repo
    filler = "填" * 80
    doc = _write(root, "docs/x.md", f"执行 `scripts/old.py` {filler} 该说法不存在。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1
    assert not issues[0].get("waived")


def test_existing_path_not_affected_by_negation(repo):
    """路径真实存在时本就不该上报，语义词不得制造额外噪声。"""
    module, root = repo
    _write(root, "scripts/real.py", "print(1)\n")
    doc = _write(root, "docs/x.md", "`scripts/real.py` 这个路径不存在吗？不，它在。\n")
    assert module.check_repo_paths([doc]) == []


def test_git_history_pointer_waived(repo):
    """回归背景：docs/deploy/pitfalls/_index.md:38 「旧版全局编号注册表见 git 历史
    `docs/pitfalls/_index.md`」——文件已迁目录，句子的正当指向就是 git 历史。"""
    module, root = repo
    doc = _write(root, "docs/pitfalls/_index.md", "旧版注册表见 git 历史 `docs/old/_index.md`。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1 and issues[0].get("waived")
    assert "git 历史" in issues[0]["waive_reason"]


def test_git_history_outside_window_reported(repo):
    module, root = repo
    filler = "说" * 80
    doc = _write(root, "docs/x.md", f"先读 `docs/old/a.md` {filler}，详情见 git 历史。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1 and not issues[0].get("waived")


# ─────────────────────── 4. .gitignore 刻意不入库的路径 ───────────────────────


def test_gitignored_path_waived_with_rule(repo_with_git):
    """回归背景：docs/deploy/部署指南.md:938 让运维「配置 `deploy/env/secrets.env`」，
    该文件被 .gitignore:73 显式排除，在任何检出里都不存在，却是一条完全正确的指令。"""
    module, root = repo_with_git
    _write(root, ".gitignore", "deploy/env/secrets.env\n")
    doc = _write(root, "docs/deploy/x.md", "配置 `deploy/env/secrets.env` 中的密钥。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1
    assert issues[0]["waived"]
    assert ".gitignore" in issues[0]["waive_reason"]


def test_non_gitignored_bad_path_reported(repo_with_git):
    """新豁免不得外溢：同一仓库里未被忽略的坏路径必须照常阻断。"""
    module, root = repo_with_git
    _write(root, ".gitignore", "deploy/env/secrets.env\n")
    doc = _write(root, "docs/deploy/x.md", "执行 `scripts/db/seed_tools.py` 初始化。\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1 and not issues[0].get("waived")


def test_non_git_repo_not_waived(repo):
    """单测临时目录不是 git 仓库，gitignore 判定必须保守返回 None。"""
    module, _root = repo
    assert module.gitignore_rule("deploy/env/secrets.env") is None


# ─────────────────────── 5. archived 文档整体豁免 ───────────────────────

ARCHIVED_FM = (
    "---\nstatus: archived\ncategory: solution\naudience: developer\nlast_updated: 2026-10-25\nowner: team\n---\n\n"
)
ACTIVE_FM = (
    "---\nstatus: active\ncategory: solution\naudience: developer\nlast_updated: 2026-10-25\nowner: team\n---\n\n"
)


def test_archived_doc_links_not_blocking(repo):
    """回归背景：docs/solution/knowledge-base/评分机制.md 已按 §7.3.1 标为
    archived 并写明废弃依据，它所引的 RatingCard.vue 等文件随 #1260 一并删除，
    要求这些路径存在等于逼已作废的文档说谎。"""
    module, root = repo
    doc = _write(
        root,
        "docs/solution/评分机制.md",
        ARCHIVED_FM + "见 [卡片](x.md) 与 `frontend/customer/src/components/RatingCard.vue`\n",
    )
    for issues in (module.check_internal_links([doc]), module.check_repo_paths([doc])):
        assert len(issues) == 1
        assert issues[0]["waived"]
        assert "archived" in issues[0]["waive_reason"]


def test_active_doc_links_still_reported(repo):
    """豁免只对 archived 生效；带 active frontmatter 的文档不得误入豁免。"""
    module, root = repo
    doc = _write(root, "docs/solution/现役.md", ACTIVE_FM + "见 [卡片](x.md)\n")
    issues = module.check_internal_links([doc])
    assert len(issues) == 1
    assert not issues[0].get("waived")


def test_doc_without_frontmatter_not_waived(repo):
    """缺失 status 时保守当 active 处理，不得因为读不到字段就放宽。"""
    module, root = repo
    doc = _write(root, "docs/solution/裸文档.md", "见 [卡片](x.md)\n")
    issues = module.check_internal_links([doc])
    assert len(issues) == 1
    assert not issues[0].get("waived")


def test_archived_waiver_must_be_itemized(repo):
    """隐式降级等于门禁空转，豁免记录必须带可审计理由。"""
    module, root = repo
    doc = _write(root, "docs/a.md", ARCHIVED_FM + "见 [x](y.md)\n")
    issues = module.check_internal_links([doc])
    assert issues and all(i.get("waived") and i.get("waive_reason") for i in issues)


def test_secrets_not_waived_by_archived(repo):
    """安全红线不区分文档状态。"""
    module, root = repo
    doc = _write(root, "docs/a.md", ARCHIVED_FM + "识图模型密钥：sk-pj-1234567890abcdefghij1234\n")
    issues = module.check_secrets([doc])
    assert issues and not all(i.get("waived") for i in issues)


# ─────────────────────── 5. 分级门禁：现行层阻断 ───────────────────────


def _mod():
    """分级判定是纯函数，不需要临时仓库，直接取模块即可。"""
    return _load_module()


def test_current_layer_includes_plain_docs():
    module = _mod()
    assert module.is_current_layer("docs/solution/架构设计.md")
    # 仓库根的 README/AGENTS 同样是读者会照着行动的现行文档，有意纳入
    assert module.is_current_layer("README.md")
    assert module.is_current_layer("AGENTS.md")


def test_current_layer_excludes_events_and_archive():
    module = _mod()
    assert not module.is_current_layer("docs/solution/events/2026-10-26-x.md")
    assert not module.is_current_layer("docs/deploy/events/2026-04-10-y.md")
    assert not module.is_current_layer("docs/archive/2025/旧文档.md")


def test_current_layer_keeps_file_named_events_md():
    """`events` 只按目录段匹配，否则会误伤名为 events.md 的现行文档。"""
    module = _mod()
    assert module.is_current_layer("docs/solution/events.md")


def test_current_layer_normalizes_backslashes():
    """Windows 分隔符必须先归一，否则事件/归档层会漏判为现行层。"""
    module = _mod()
    assert not module.is_current_layer("docs\\solution\\events\\a.md")
    assert not module.is_current_layer("docs\\archive\\a.md")


def _rot(file: str, line: int, detail: str = "链接目标不存在") -> dict:
    return {"type": "C1", "file": file, "line": line, "detail": detail}


def test_current_blocking_blocks_stock_in_current_layer():
    """该层已归零，存量即本次新引入——不看改动行。"""
    module = _mod()
    block = [_rot("docs/solution/架构设计.md", 99)]
    changed = {"docs/solution/架构设计.md": {10}}
    blocking, extra, stock = module.select_blocking(
        block, changed, changed_only=True, fail_on_stock=False, current_blocking=True
    )
    assert [i["line"] for i in blocking] == [99]
    assert len(extra) == 1
    assert stock == 0


def test_current_blocking_keeps_events_layer_advisory():
    """史料不得为了 CI 变绿而改写，这是 §8.3 的硬约束。"""
    module = _mod()
    block = [_rot("docs/solution/events/2026-04-10-x.md", 99)]
    changed = {"docs/solution/events/2026-04-10-x.md": {10}}
    blocking, extra, stock = module.select_blocking(
        block, changed, changed_only=True, fail_on_stock=False, current_blocking=True
    )
    assert blocking == []
    assert extra == []
    assert stock == 1


def test_without_current_blocking_matches_legacy_behaviour():
    """默认路径不得被新开关偷偷收紧——否则等于绕过评审改了门禁。"""
    module = _mod()
    block = [_rot("docs/solution/架构设计.md", 99), _rot("docs/solution/events/x.md", 5)]
    changed = {"docs/solution/架构设计.md": {10}}
    blocking, extra, stock = module.select_blocking(
        block, changed, changed_only=True, fail_on_stock=False, current_blocking=False
    )
    assert blocking == []
    assert extra == []
    assert stock == 2


def test_changed_line_hit_blocked_once_only():
    """同一条既是改动行又在现行层，只能出现在阻断清单里一次。"""
    module = _mod()
    block = [_rot("docs/solution/架构设计.md", 10)]
    changed = {"docs/solution/架构设计.md": {10}}
    blocking, extra, stock = module.select_blocking(
        block, changed, changed_only=True, fail_on_stock=False, current_blocking=True
    )
    assert len(blocking) == 1
    assert extra == []
    assert stock == 0


def test_full_scan_mode_blocks_all_stock():
    """非增量（--fail-on-stock 或全量扫描）时，现行层开关不改变既有语义。"""
    module = _mod()
    block = [_rot("docs/solution/架构设计.md", 99), _rot("docs/solution/events/x.md", 5)]
    blocking, extra, stock = module.select_blocking(
        block, None, changed_only=False, fail_on_stock=True, current_blocking=True
    )
    assert len(blocking) == 2
    assert stock == 0


def test_none_changed_lines_blocks_only_current_layer():
    """取不到改动行时的兜底：现行层与来源无关仍阻断，史料层不误伤。"""
    module = _mod()
    block = [_rot("docs/solution/架构设计.md", 99), _rot("docs/solution/events/x.md", 5)]
    blocking, extra, stock = module.select_blocking(
        block, None, changed_only=True, fail_on_stock=False, current_blocking=True
    )
    assert [i["file"] for i in blocking] == ["docs/solution/架构设计.md"]
    assert stock == 1


def test_same_line_negation_escapes_adjacent_bad_path(repo):
    """规范 §九 写明：否定窗口是启发式，同一行先写一个「不存在」就能豁免邻近坏路径。

    本用例不是「希望如此」，而是把这个逃逸面钉成显式行为：将来若有人收紧窗口，
    这条用例会失败并强迫他同步改规范；若有人把窗口放大到跨行，同样会被下面
    test_negation_word_outside_window_reported 拦住。
    """
    module, root = repo
    doc = _write(root, "docs/a.md", "见 [不存在的文档](missing.md) 与路径 `scripts/not-exist-zzz.py`\n")
    issues = module.check_repo_paths([doc])
    assert len(issues) == 1
    assert issues[0].get("waived")
    assert "不存在" in issues[0]["waive_reason"]
