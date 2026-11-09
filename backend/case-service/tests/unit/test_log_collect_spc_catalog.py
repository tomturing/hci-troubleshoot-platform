"""spc 目录快照的解析契约。"""

from app.services.log_collect_spc_catalog import load_catalog


def _items(catalog, module):
    mod = {m["module"]: m for m in catalog["modules"]}[module]
    return [i for g in mod["groups"] for i in g["items"]]


def test_catalog_covers_all_snapshots():
    catalog = load_catalog()
    modules = {m["module"]: m for m in catalog["modules"]}
    # 11 份：const_modules 的 10 个 + 只走 -s 的 compute
    assert len(modules) == 11
    assert modules["compute"]["m_whitelisted"] is False
    assert modules["other"]["m_whitelisted"] is True
    assert catalog["total_items"] > 0


def test_wildcard_and_filters_are_not_config_usable():
    catalog = load_catalog()
    items = _items(catalog, "other")
    wildcarded = [i for i in items if i["has_wildcard"]]
    assert wildcarded, "other 应包含通配条目"
    assert all(not i["usable_by_config"] for i in wildcarded)
    # 带 filters 的同样不可用：-c 的 cp -r 不认过滤条件
    filtered = [i for i in items if i["filters"]]
    assert filtered
    assert all(not i["usable_by_config"] for i in filtered)


def test_script_module_is_not_config_usable():
    catalog = load_catalog()
    modules = {m["module"]: m for m in catalog["modules"]}
    for name in ("network", "storage", "slowdisk"):
        items = [i for g in modules[name]["groups"] for i in g["items"]]
        assert items and all(i["action"] == "script" for i in items)
        assert all(not i["usable_by_config"] for i in items)


def test_date_placeholder_is_kept_and_flagged():
    catalog = load_catalog()
    items = _items(catalog, "other")
    dated = [i for i in items if i["needs_date_expand"]]
    assert dated
    formats = {d["format"] for i in dated for d in i["date_placeholders"]}
    assert formats == {"%d", "%Y%m%d"}
