from resolve_main_test_scope import resolve_gate_scope


def test_empty_change_list_requires_full_backend_gate() -> None:
    assert resolve_gate_scope([]).required is True


def test_blank_only_change_list_requires_full_backend_gate() -> None:
    assert resolve_gate_scope(["", "   "]).required is True


def test_frontend_only_change_skips_backend_gate() -> None:
    scope = resolve_gate_scope(["frontend/admin/src/views/KbdReviewView.vue"])
    assert scope.required is False


def test_frontend_with_lockfile_still_skips_backend_gate() -> None:
    # 前端依赖变更由 frontend-check 的完整回归负责，不进入后端门禁输入。
    paths = ["frontend/pnpm-lock.yaml", "frontend/package.json"]
    assert resolve_gate_scope(paths).required is False


def test_docs_and_root_markdown_skip_backend_gate() -> None:
    paths = ["docs/solution/events/示例.md", "README.md", "AGENTS.md", "CLAUDE.md"]
    assert resolve_gate_scope(paths).required is False


def test_backend_change_requires_backend_gate() -> None:
    assert resolve_gate_scope(["backend/shared/signals/models.py"]).required is True


def test_tests_change_requires_backend_gate() -> None:
    assert resolve_gate_scope(["tests/unit/test_foo.py"]).required is True


def test_deploy_change_requires_backend_gate() -> None:
    # unit-tests 中的 REQUIRE_HELM 配置契约校验会读取 deploy/，不得跳过。
    assert resolve_gate_scope(["deploy/helm/hci-platform/values.yaml"]).required is True


def test_ci_config_change_requires_backend_gate() -> None:
    assert resolve_gate_scope([".github/workflows/ci.yml"]).required is True


def test_scripts_change_requires_backend_gate() -> None:
    assert resolve_gate_scope(["scripts/ci/resolve_image_build_plan.py"]).required is True


def test_database_change_requires_backend_gate() -> None:
    assert resolve_gate_scope(["database/schema.hcl"]).required is True


def test_unknown_root_file_requires_backend_gate() -> None:
    assert resolve_gate_scope([".env.example"]).required is True


def test_mixed_change_requires_backend_gate_and_reports_path() -> None:
    scope = resolve_gate_scope(["frontend/admin/src/main.ts", "backend/api-gateway/app/main.py"])
    assert scope.required is True
    assert "backend/api-gateway/app/main.py" in scope.reason


def test_reason_mentions_whitelist_when_skipping() -> None:
    scope = resolve_gate_scope(["frontend/admin/src/main.ts"])
    assert "白名单" in scope.reason
