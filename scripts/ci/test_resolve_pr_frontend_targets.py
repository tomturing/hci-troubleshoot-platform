from resolve_pr_frontend_targets import resolve_frontend_plan


def test_admin_changes_are_targeted() -> None:
    plan = resolve_frontend_plan(["frontend/admin/src/App.vue"])
    assert plan.scope == "admin"


def test_customer_changes_are_targeted() -> None:
    plan = resolve_frontend_plan(["frontend/customer/src/App.vue"])
    assert plan.scope == "customer"


def test_shared_and_lockfile_changes_are_full() -> None:
    assert resolve_frontend_plan(["frontend/shared/src/index.ts"]).scope == "full"
    assert resolve_frontend_plan(["frontend/pnpm-lock.yaml"]).scope == "full"


def test_unknown_frontend_changes_are_full() -> None:
    assert resolve_frontend_plan(["frontend/vite.config.ts"]).scope == "full"


def test_both_apps_are_full() -> None:
    plan = resolve_frontend_plan(["frontend/admin/src/App.vue", "frontend/customer/src/App.vue"])
    assert plan.scope == "full"


def test_non_frontend_changes_skip_frontend() -> None:
    assert resolve_frontend_plan(["backend/app.py", "docs/README.md"]).scope == "none"
