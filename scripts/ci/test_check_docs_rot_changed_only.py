"""check_docs_rot 增量模式路径解析的回归测试。

核心防护目标：增量门禁必须能识别**非 ASCII（中文）路径**的改动文档。

回归背景：`list_changed_md_files` 的前身直接调用 `git diff --name-only`。
在 core.quotepath=true（git 缺省值）下，该命令把非 ASCII 路径输出为带引号
的八进制转义串（如 "docs/solution/\\346\\236\\266..."），拼成 Path 后指向
不存在的文件，于是「改动文档」被过滤成空集，门禁静默打印
「增量模式：仅检查 0 个改动文档」并输出「✅ 门禁通过」。

危害不止于文档腐化无人拦截：C6 密钥泄露扫描同样以 files 为输入，
因此把密钥写进中文命名的 .md 文件也能通过门禁。本仓库文档绝大多数是
中文路径，命中率接近 100%，该缺陷因此长期掩盖。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "check_docs_rot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_rot", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _init_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """在 tmp_path 建一个已提交 files 的 git 仓库，返回仓库路径。"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "ci@example.com")
    _git(tmp_path, "config", "user.name", "ci")
    # 显式固定为 git 缺省值，避免运行者全局配置干扰本组测试的因果判定。
    _git(tmp_path, "config", "core.quotepath", "true")
    _commit(tmp_path, files, "initial")
    return tmp_path


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_single_non_ascii_doc_is_returned_verbatim(tmp_path):
    """单个中文命名文档改动必须原样返回，且能拼成真实存在的文件。"""
    repo = _init_repo(tmp_path, {"docs/架构设计.md": "# 版本 1\n"})
    _commit(repo, {"docs/架构设计.md": "# 版本 2\n"}, "改文档")

    module = _load_module()
    changed = module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo)

    assert changed == ["docs/架构设计.md"]
    # 关键断言：转义串在这里必然失败——这正是旧实现空转的直接原因。
    assert (repo / changed[0]).is_file()


def test_multiple_non_ascii_docs_and_directory_changes(tmp_path):
    """多层级中文路径（含子目录）必须全部识别，一项不漏。"""
    repo = _init_repo(
        tmp_path,
        {
            "docs/solution/架构设计.md": "# 架构\n",
            "docs/solution/数据库设计.md": "# 数据库\n",
            "docs/deploy/部署设计.md": "# 部署\n",
        },
    )
    _commit(
        repo,
        {
            "docs/solution/架构设计.md": "# 架构 v2\n",
            "docs/solution/数据库设计.md": "# 数据库 v2\n",
            "docs/deploy/部署设计.md": "# 部署 v2\n",
        },
        "改三份文档",
    )

    module = _load_module()
    changed = module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo)

    # 用集合比较：中文路径的字典序按 Unicode 码点，不适合做顺序断言。
    assert set(changed) == {
        "docs/deploy/部署设计.md",
        "docs/solution/架构设计.md",
        "docs/solution/数据库设计.md",
    }
    for rel in changed:
        assert (repo / rel).is_file()


def test_only_markdown_files_are_listed(tmp_path):
    """非 .md 文件（含中文命名的 .py）不应进入增量检查范围。"""
    repo = _init_repo(
        tmp_path,
        {"docs/架构设计.md": "# 架构\n", "scripts/中文脚本.py": "print(1)\n"},
    )
    _commit(
        repo,
        {"docs/架构设计.md": "# 架构 v2\n", "scripts/中文脚本.py": "print(2)\n"},
        "同时改 md 与 py",
    )

    module = _load_module()
    changed = module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo)

    assert changed == ["docs/架构设计.md"]


def test_deleted_md_excluded_while_modified_md_kept(tmp_path):
    """删除的文档必须排除，修改的文档必须保留。

    被删除的文档不可能出现在工作区遍历结果里。若将其计入「git 报告的
    改动数」，增量门禁的「命中数 == 改动数」fail-closed 断言就会误报，
    所以 `list_changed_md_files` 固定使用 `--diff-filter=d`。
    """
    repo = _init_repo(tmp_path, {"docs/旧文档.md": "# 旧\n", "docs/新文档.md": "# 新\n"})
    (repo / "docs/旧文档.md").unlink()
    (repo / "docs/新文档.md").write_text("# 新 v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "删旧改新")

    module = _load_module()
    changed = module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo)

    assert changed == ["docs/新文档.md"]


def test_plain_name_only_would_escape_non_ascii(tmp_path):
    """反证 bug 因果：不带 -z 时 git 确实会转义中文路径。

    这条测试锁定了 `-z` 的必要性。若未来 git 改变了该默认行为、
    或仓库移除了全部非 ASCII 路径，本测试会失败，届时应重新评估
    `list_changed_md_files` 的注释与实现是否仍需要 `-z`。
    """
    repo = _init_repo(tmp_path, {"docs/架构设计.md": "# 版本 1\n"})
    _commit(repo, {"docs/架构设计.md": "# 版本 2\n"}, "改文档")

    raw = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").strip()

    # 被双引号包裹，且内容为八进制转义，而非可直接使用的路径。
    assert raw.startswith('"') and raw.endswith('"')
    assert "\\346" in raw
    # 这种串拼成 Path 后不存在——空转由此产生。
    assert not (repo / raw).exists()


def test_fix_produces_path_that_resolves_unlike_escaped_form(tmp_path):
    """对照断言：修复后的返回值可解析，转义串不可解析。"""
    repo = _init_repo(tmp_path, {"docs/知识库设计.md": "# v1\n"})
    _commit(repo, {"docs/知识库设计.md": "# v2\n"}, "改文档")

    module = _load_module()
    fixed = module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo)
    escaped = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").strip()

    assert (repo / fixed[0]).exists()
    assert not (repo / escaped).exists()


# ---------------------------------------------------------------------------
# 行号解析：用于区分「本次改动引入的腐化」与「文档存量腐化」
# ---------------------------------------------------------------------------


def test_changed_lines_marks_only_modified_line(tmp_path):
    """只改第 2 行时，行号集合只应包含第 2 行。

    这是「按行精确阻断」的地基：若行号算错（多报），维护者改一个 typo
    就会被追着修整篇文档的历史死链，结果是大家不敢改文档。
    """
    repo = _init_repo(tmp_path, {"docs/文档.md": "第1行\n第2行\n第3行\n"})
    _commit(repo, {"docs/文档.md": "第1行\n第2行（改）\n第3行\n"}, "改第2行")

    module = _load_module()
    lines = module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo)

    assert lines == {"docs/文档.md": {2}}


def test_changed_lines_covers_appended_lines(tmp_path):
    """在文末追加两行时，应覆盖新增的两行行号。"""
    repo = _init_repo(tmp_path, {"docs/文档.md": "第1行\n"})
    _commit(repo, {"docs/文档.md": "第1行\n第2行\n第3行\n"}, "追加两行")

    module = _load_module()
    lines = module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo)

    assert lines == {"docs/文档.md": {2, 3}}


def test_changed_lines_empty_for_pure_deletion(tmp_path):
    """纯删行不产生新增区间，因此不会阻断任何问题。"""
    repo = _init_repo(tmp_path, {"docs/文档.md": "第1行\n第2行\n第3行\n"})
    _commit(repo, {"docs/文档.md": "第1行\n第3行\n"}, "删第2行")

    module = _load_module()
    lines = module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo)

    # 文件出现在映射中（用于一致性校验），但无新增行
    assert lines == {"docs/文档.md": set()}


def test_changed_lines_handles_multiple_non_ascii_files(tmp_path):
    """多个中文文档同时改动时，各自的行号映射必须独立正确。"""
    repo = _init_repo(
        tmp_path,
        {"docs/架构设计.md": "A1\nA2\n", "docs/数据库设计.md": "B1\nB2\n"},
    )
    _commit(
        repo,
        {"docs/架构设计.md": "A1（改）\nA2\n", "docs/数据库设计.md": "B1\nB2（改）\n"},
        "两篇都改",
    )

    module = _load_module()
    lines = module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo)

    assert lines == {"docs/架构设计.md": {1}, "docs/数据库设计.md": {2}}


def test_changed_lines_file_set_matches_changed_files(tmp_path):
    """两个解析器（`-z` 文件名 / hunk 头）必须看到同一批文件。

    调用方依赖两者一致来 fail-closed：若 hunk 头路径被 quoting 污染，
    行号映射不可信，宁可报错也不能静默只警告。
    """
    repo = _init_repo(tmp_path, {"docs/架构设计.md": "A1\n", "docs/部署设计.md": "C1\n"})
    _commit(
        repo,
        {"docs/架构设计.md": "A1（改）\n", "docs/部署设计.md": "C1（改）\n"},
        "两篇都改",
    )

    module = _load_module()
    names = set(module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo))
    line_keys = set(module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo))

    assert names == line_keys == {"docs/架构设计.md", "docs/部署设计.md"}


# ---------------------------------------------------------------------------
# 纯改名：不产生 hunk 头，不能因此被判成「丢文件」
# ---------------------------------------------------------------------------


def test_pure_rename_produces_no_hunk_header(tmp_path):
    """相似度 100% 的改名不产生 hunk 头，这是 git 的正常行为而非解析缺陷。

    回归背景：增量门禁曾用「改动文件全集 == hunk 头文件集」做相等断言。
    一次「18 个纯改名 + 9 个内容改动」的文档瘦身 PR 因此 fail-closed：
    改动文件 26 个、hunk 头只有 9 个，看起来像路径 quoting 回归，实际完全正常。

    `list_content_changed_md_files` 用 `--numstat -z` 取「真有几行变化」的集合，
    纯改名 add/del 恒为 0，因此天然被排除在断言对象之外。
    """
    repo = _init_repo(tmp_path, {"docs/旧位置.md": "# 同一份内容\n"})
    _git(repo, "mv", "docs/旧位置.md", "docs/新位置.md")
    _git(repo, "commit", "-q", "-m", "纯改名")

    module = _load_module()
    names = module.list_changed_md_files("HEAD~1", "HEAD", cwd=repo)
    line_keys = set(module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo))
    content = module.list_content_changed_md_files("HEAD~1", "HEAD", cwd=repo)

    # 改名后的新路径仍在扫描范围内（门禁依旧会检查它）
    assert names == ["docs/新位置.md"]
    # hunk 头为空——旧断言误报的直接原因
    assert line_keys == set()
    # 「有内容变化」同样为空，两者一致，门禁不再误报
    assert content == set()


def test_rename_with_content_edit_counts_as_content_changed(tmp_path):
    """既改名又改内容：必须计入「有内容变化」集合，且行号归因到新路径。

    场景取「五行改一行」以保证相似度足够高，git 才会按「改名+局部改动」输出；
    若相似度掉到阈值以下，git 会整文件重写，行号断言就失去意义。
    """
    repo = _init_repo(tmp_path, {"docs/旧位置.md": "A1\nA2\nA3\nA4\nA5\n"})
    _git(repo, "mv", "docs/旧位置.md", "docs/新位置.md")
    (repo / "docs/新位置.md").write_text("A1\nA2\nA3（改）\nA4\nA5\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "改名并改内容")

    module = _load_module()

    assert module.list_content_changed_md_files("HEAD~1", "HEAD", cwd=repo) == {"docs/新位置.md"}
    assert module.list_changed_md_lines("HEAD~1", "HEAD", cwd=repo) == {"docs/新位置.md": {3}}
