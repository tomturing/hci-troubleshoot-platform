"""晋级脚本与镜像构建矩阵的服务 key 契约。

背景（真实故障）：新增服务时只把 deploy key 登记进镜像构建矩阵
``scripts/ci/resolve_image_build_plan.py``，却忘了同步登记到晋级脚本
``scripts/ops/sync-env-repo-tags.sh`` 的 ``service_repository()`` 映射，
会导致发布期晋级以"未知服务 key"中断，**整批服务的 image tag 都不再更新**
（线上静默停在旧版本，前端看起来"没问题"其实根本没发布）。

本契约把两处绑定：凡是镜像构建计划会输出的 deploy key，晋级脚本必须认得。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_SCRIPT = REPO_ROOT / "scripts" / "ci" / "resolve_image_build_plan.py"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "ops" / "sync-env-repo-tags.sh"


def _load_plan_module():
    spec = importlib.util.spec_from_file_location("resolve_image_build_plan", PLAN_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deploy_keys() -> set[str]:
    """镜像构建矩阵会写入环境仓库的 deploy key 集合。"""
    module = _load_plan_module()
    return {key for _, _, key in module.ALL_SERVICES if key}


def sync_script_keys() -> set[str]:
    """晋级脚本 service_repository() 的 case 分支标签集合。"""
    content = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert "service_repository()" in content, "晋级脚本结构变更，契约需同步调整"
    case_block = content.split("service_repository() {", 1)[1]
    return set(re.findall(r"^\s{4}([A-Za-z][A-Za-z0-9]*)\)\s", case_block, flags=re.M))


def test_env_repo_sync_script_knows_every_deploy_key() -> None:
    """镜像矩阵的 deploy key 必须都能被晋级脚本解析，否则发布会被中断。"""
    missing = deploy_keys() - sync_script_keys()
    assert not missing, (
        f"{SYNC_SCRIPT.name} 缺少服务 key 映射：{sorted(missing)}；新增服务必须同时更新镜像构建矩阵与晋级脚本"
    )


def test_contract_scripts_are_reachable() -> None:
    """防止脚本改名/挪位导致契约测试变成空集合而失效。"""
    keys = deploy_keys()
    assert len(keys) >= 10, f"deploy key 过少，可能是矩阵解析失败：{sorted(keys)}"
    assert len(sync_script_keys()) >= 10, "晋级脚本 case 分支过少，可能正则已失效"
