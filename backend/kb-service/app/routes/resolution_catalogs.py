"""KB Service — Shared Resolution Catalogs 管理路由

提供后端 Shared Resolution Catalog (JSON) 的查询、在线编辑写回、格式校验与导入导出能力。
写入磁盘时自动更新文件 mtime，触发 shared.resolution.catalog 热加载机制即刻生效。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from shared.resolution.catalog import (
    ACLI_CATALOG_PATH,
    RESOLUTION_CATALOG_PATH,
    load_acli_catalog,
    load_resolution_catalog,
)

logger = get_logger("kb-service-resolution-catalogs")
router = APIRouter(prefix="/api/kb/resolution-catalogs", tags=["resolution-catalogs"])

# 允许访问与管理的配置文件集合
_ALLOWED_CATALOGS: dict[str, dict[str, Any]] = {
    "acli_command_catalog.json": {
        "title": "aCLI 命令目录 Catalog",
        "description": "定义系统支持的全部 aCLI 命令标准路径与参数，用于指令安全与语义审查",
        "path": ACLI_CATALOG_PATH,
    },
    "resolution_catalog.json": {
        "title": "Shared Resolution Runtime Catalog",
        "description": "包含 Log 日志别名、Domain 命令规范及 QKV Action 关联映射规则",
        "path": RESOLUTION_CATALOG_PATH,
    },
}


class CatalogSaveRequest(BaseModel):
    content: str = Field(..., description="完整的 JSON 字符串内容")


def _get_catalog_meta(name: str, config: dict[str, Any]) -> dict[str, Any]:
    path: Path = config["path"]
    size_bytes = 0
    mtime_iso = ""
    item_count = 0
    exists = path.exists()

    if exists:
        try:
            stat = path.stat()
            size_bytes = stat.st_size
            mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
        except OSError:
            pass

    if name == "acli_command_catalog.json":
        item_count = len(load_acli_catalog())
    elif name == "resolution_catalog.json":
        res_data = load_resolution_catalog()
        # 统计规则总条数
        log_alias_count = len(res_data.get("log_aliases", {})) if isinstance(res_data.get("log_aliases"), dict) else 0
        domain_req_count = len(res_data.get("domain_command_requirements", [])) if isinstance(res_data.get("domain_command_requirements"), list) else 0
        qkv_act_count = len(res_data.get("qkv_actions", [])) if isinstance(res_data.get("qkv_actions"), list) else 0
        item_count = log_alias_count + domain_req_count + qkv_act_count

    return {
        "name": name,
        "title": config["title"],
        "description": config["description"],
        "exists": exists,
        "size_bytes": size_bytes,
        "mtime": mtime_iso,
        "item_count": item_count,
    }


@router.get("", summary="获取所有可管理的 Resolution Catalog 列表")
async def list_catalogs() -> dict[str, Any]:
    catalogs = [_get_catalog_meta(name, cfg) for name, cfg in _ALLOWED_CATALOGS.items()]
    return {"catalogs": catalogs}


@router.get("/{filename}", summary="获取指定 Catalog 的 JSON 内容与元数据")
async def get_catalog(filename: str) -> dict[str, Any]:
    if filename not in _ALLOWED_CATALOGS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"不支持的 Catalog 文件: {filename}。仅允许管理: {list(_ALLOWED_CATALOGS.keys())}",
        )

    cfg = _ALLOWED_CATALOGS[filename]
    path: Path = cfg["path"]
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Catalog 文件不存在: {filename}",
        )

    try:
        raw_text = path.read_text(encoding="utf-8")
        parsed_json = json.loads(raw_text)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"读取或解析 Catalog 文件失败: {exc}",
        )

    meta = _get_catalog_meta(filename, cfg)
    return {
        "meta": meta,
        "content_text": raw_text,
        "content_json": parsed_json,
    }


@router.post("/{filename}/validate", summary="校验 JSON 内容语法与 Schema 合法性")
async def validate_catalog(filename: str, body: CatalogSaveRequest) -> dict[str, Any]:
    if filename not in _ALLOWED_CATALOGS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"不支持的 Catalog 文件: {filename}",
        )

    try:
        data = json.loads(body.content)
    except json.JSONDecodeError as exc:
        return {
            "valid": False,
            "error_type": "JSONDecodeError",
            "message": f"JSON 语法错误 (第 {exc.lineno} 行，第 {exc.colno} 列): {exc.msg}",
        }

    # 针对不同 catalog 做基础 schema 语义判断
    if filename == "acli_command_catalog.json":
        if not isinstance(data, dict) or "commands" not in data or not isinstance(data.get("commands"), list):
            return {
                "valid": False,
                "error_type": "SchemaError",
                "message": "acli_command_catalog 根节点必须是包含 'commands' 数组的对象",
            }
        item_count = len(data["commands"])
        return {
            "valid": True,
            "message": f"JSON 语法合法，包含 {item_count} 条 aCLI 命令规则",
            "item_count": item_count,
        }
    elif filename == "resolution_catalog.json":
        if not isinstance(data, dict):
            return {
                "valid": False,
                "error_type": "SchemaError",
                "message": "resolution_catalog 根节点必须是 JSON 对象",
            }
        log_alias_count = len(data.get("log_aliases", {})) if isinstance(data.get("log_aliases"), dict) else 0
        domain_req_count = len(data.get("domain_command_requirements", [])) if isinstance(data.get("domain_command_requirements"), list) else 0
        qkv_act_count = len(data.get("qkv_actions", [])) if isinstance(data.get("qkv_actions"), list) else 0
        total = log_alias_count + domain_req_count + qkv_act_count
        return {
            "valid": True,
            "message": f"JSON 语法合法 (含 {log_alias_count} 条 Log 别名、{domain_req_count} 条 Domain 契约、{qkv_act_count} 条 QKV Action)",
            "item_count": total,
        }

    return {"valid": True, "message": "JSON 语法校验通过"}


@router.put("/{filename}", summary="更新指定 Catalog 内容（在线保存，即时触发热加载）")
async def update_catalog(filename: str, body: CatalogSaveRequest) -> dict[str, Any]:
    if filename not in _ALLOWED_CATALOGS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"不支持的 Catalog 文件: {filename}",
        )

    # 先做校验
    val_res = await validate_catalog(filename, body)
    if not val_res.get("valid"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无法保存：{val_res.get('message')}",
        )

    cfg = _ALLOWED_CATALOGS[filename]
    path: Path = cfg["path"]

    try:
        # 统一以 2 空格美化格式写入磁盘，保持 JSON 排版整洁
        formatted_json = json.dumps(json.loads(body.content), ensure_ascii=False, indent=2) + "\n"
        path.write_text(formatted_json, encoding="utf-8")
        logger.info(
            event="catalog_saved_and_hot_reloaded",
            filename=filename,
            path=str(path),
            size=len(formatted_json),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入 Catalog 文件失败: {exc}",
        )

    # 触发强行重新加载验证
    if filename == "acli_command_catalog.json":
        reloaded_count = len(load_acli_catalog())
    else:
        load_resolution_catalog()
        reloaded_count = val_res.get("item_count", 0)

    meta = _get_catalog_meta(filename, cfg)
    return {
        "success": True,
        "message": f"保存成功！后端的 Shared Resolution Runtime 已自动感知 mtime 变更并热加载生效 (当前记录: {reloaded_count})",
        "meta": meta,
        "content_text": formatted_json,
    }
