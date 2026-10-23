"""日志收集错误码目录的路径解析与降级行为。

回归背景：镜像布局 `/app/app/schemas/` 与仓库布局 `backend/case-service/app/schemas/`
的父目录层数不同，固定 `parents[3]` 在镜像里算成 `/shared/...`，导致 409 冲突响应
构建 Problem 时抛 `FileNotFoundError` → 500（staging 实测）。
"""

from app.schemas import log_collect_errors as err


def test_catalog_resolves_and_contains_plugin_conflict():
    """路径必须能定位到真实目录文件（仓库/镜像两种布局都适用）。"""
    assert err.CATALOG_PATH.is_file()
    catalog = err.load_error_catalog()
    assert "LC-PLUGIN-007" in catalog


def test_missing_catalog_degrades_instead_of_raising(monkeypatch, tmp_path):
    """目录缺失时返回空映射，build_problem 降级为通用结构而不是 500 异常。"""
    monkeypatch.setattr(err, "CATALOG_PATH", tmp_path / "missing.json")
    err.load_error_catalog.cache_clear()
    try:
        assert err.load_error_catalog() == {}
        problem = err.build_problem("LC-PLUGIN-007", "该工单已有进行中的日志收集任务")
        assert problem["code"] == "LC-PLUGIN-007"
        assert problem["status"] == 500
        assert problem["detail"] == "该工单已有进行中的日志收集任务"
    finally:
        err.load_error_catalog.cache_clear()
