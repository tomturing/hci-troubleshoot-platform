"""check_argocd_app_sync_invariants 的回归测试。

防护目标：**带 automated.selfHeal 的 Application 必须显式声明 syncPolicy.retry**。

回归背景（2026-10-29）：迁移 hook Job 因依赖 Secret 缺失卡在
CreateContainerConfigError，Pod 永远不失败，于是 hook-delete-policy 的
HookFailed 自愈链从未启动，staging 同步永久挂起 8 分钟以上。selfHeal 只
恢复漂移、不重试失败操作，因此必须显式配置 retry 才能让瞬态故障自动收敛。
本组测试把这条不变式钉住，防止清单回退。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "check_argocd_app_sync_invariants.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_argocd_app_sync_invariants", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content, encoding="utf-8")


_APP_TEMPLATE = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
spec:
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
{sync_rest}"""

_RETRY = """\
    retry:
      limit: 5
"""


def test_self_heal_without_retry_is_reported(tmp_path):
    module = _load_module()
    _write(tmp_path, "app.yaml", _APP_TEMPLATE.format(name="demo", sync_rest=""))

    violations = module.collect_violations(tmp_path)

    assert len(violations) == 1
    assert "demo" in violations[0]
    assert "retry" in violations[0]


def test_self_heal_with_retry_passes(tmp_path):
    module = _load_module()
    _write(tmp_path, "app.yaml", _APP_TEMPLATE.format(name="demo", sync_rest=_RETRY))

    assert module.collect_violations(tmp_path) == []


def test_retry_limit_below_threshold_is_reported(tmp_path):
    module = _load_module()
    weak = "    retry:\n      limit: 1\n"
    _write(tmp_path, "app.yaml", _APP_TEMPLATE.format(name="demo", sync_rest=weak))

    violations = module.collect_violations(tmp_path)

    assert len(violations) == 1
    assert "limit" in violations[0]


def test_application_without_self_heal_needs_no_retry(tmp_path):
    """生产环境手动同步（无 automated）不要求 retry，避免把规则滥用到人工闸门上。"""
    module = _load_module()
    _write(
        tmp_path,
        "manual.yaml",
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        "  name: prod-manual\n"
        "spec:\n"
        "  syncPolicy:\n"
        "    syncOptions:\n"
        "      - CreateNamespace=true\n",
    )

    assert module.collect_violations(tmp_path) == []


def test_document_order_multi_app_file(tmp_path):
    """同一文件内多个 Application 逐一校验（argo-apps 存在多文档清单）。"""
    module = _load_module()
    good = _APP_TEMPLATE.format(name="good", sync_rest=_RETRY)
    bad = _APP_TEMPLATE.format(name="bad", sync_rest="")
    _write(tmp_path, "multi.yaml", good + "---\n" + bad)

    violations = module.collect_violations(tmp_path)

    assert len(violations) == 1
    assert "bad" in violations[0]


def test_non_application_documents_are_ignored(tmp_path):
    module = _load_module()
    _write(
        tmp_path,
        "cm.yaml",
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
    )

    assert module.collect_violations(tmp_path) == []


def test_repository_manifests_satisfy_invariant():
    """仓库内真实清单必须全部通过（否则本规则会常年红灯）。"""
    module = _load_module()
    repo_root = Path(__file__).resolve().parents[2]
    root = repo_root / module.DEFAULT_PATH

    assert root.is_dir(), f"找不到 Application 清单目录: {root}"
    assert module.collect_violations(root) == []
