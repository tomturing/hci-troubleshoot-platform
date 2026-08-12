"""KB Service — Shared Resolution Catalogs 路由单元测试

覆盖路由：
- GET  /api/kb/resolution-catalogs
- GET  /api/kb/resolution-catalogs/{filename}
- POST /api/kb/resolution-catalogs/{filename}/validate
- PUT  /api/kb/resolution-catalogs/{filename}
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.routes.resolution_catalogs import router
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def test_list_catalogs(client: TestClient) -> None:
    """测试 GET /api/kb/resolution-catalogs 获取配置目录列表"""
    resp = client.get("/api/kb/resolution-catalogs")
    assert resp.status_code == 200

    data = resp.json()
    assert "catalogs" in data
    names = [c["name"] for c in data["catalogs"]]
    assert "acli_command_catalog.json" in names
    assert "resolution_catalog.json" in names

    # 校验字段属性
    for item in data["catalogs"]:
        assert "title" in item
        assert "description" in item
        assert "size_bytes" in item
        assert "item_count" in item
        assert item["item_count"] > 0


def test_get_catalog_detail_success(client: TestClient) -> None:
    """测试 GET /api/kb/resolution-catalogs/{filename} 获取指定配置详情"""
    resp = client.get("/api/kb/resolution-catalogs/acli_command_catalog.json")
    assert resp.status_code == 200

    data = resp.json()
    assert "meta" in data
    assert "content_text" in data
    assert "content_json" in data
    assert data["meta"]["name"] == "acli_command_catalog.json"
    assert isinstance(data["content_json"], dict)
    assert "commands" in data["content_json"]


def test_get_catalog_detail_not_found(client: TestClient) -> None:
    """测试 GET 请求不存在或非法 filename 时返回 404"""
    resp = client.get("/api/kb/resolution-catalogs/non_existent_catalog.json")
    assert resp.status_code == 404
    assert "不支持的 Catalog 文件" in resp.json()["detail"]


def test_validate_catalog_valid_json(client: TestClient) -> None:
    """测试 POST /api/kb/resolution-catalogs/{filename}/validate 语法与格式合法"""
    valid_payload = {
        "content": json.dumps({
            "schema_version": 1,
            "catalog_version": "test",
            "commands": [
                {"command": "acli system test_cmd"}
            ]
        })
    }
    resp = client.post("/api/kb/resolution-catalogs/acli_command_catalog.json/validate", json=valid_payload)
    assert resp.status_code == 200
    res = resp.json()
    assert res["valid"] is True
    assert res["item_count"] == 1


def test_validate_catalog_invalid_json_syntax(client: TestClient) -> None:
    """测试 JSON 语法错误被正确捕获"""
    invalid_payload = {
        "content": "{\"invalid_json\": [unclosed_string}"
    }
    resp = client.post("/api/kb/resolution-catalogs/acli_command_catalog.json/validate", json=invalid_payload)
    assert resp.status_code == 200
    res = resp.json()
    assert res["valid"] is False
    assert res["error_type"] == "JSONDecodeError"
    assert "JSON 语法错误" in res["message"]


def test_validate_catalog_invalid_schema(client: TestClient) -> None:
    """测试 Schema 不匹配被捕获 (如 acli catalog 缺少 commands 数组)"""
    invalid_schema = {
        "content": json.dumps({"wrong_field": 123})
    }
    resp = client.post("/api/kb/resolution-catalogs/acli_command_catalog.json/validate", json=invalid_schema)
    assert resp.status_code == 200
    res = resp.json()
    assert res["valid"] is False
    assert res["error_type"] == "SchemaError"


def test_update_catalog_success_and_hot_reload(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 PUT /api/kb/resolution-catalogs/{filename} 写入与热加载校验"""
    # 使用临时路径隔离真实磁盘文件
    fake_catalog_path = tmp_path / "resolution_catalog.json"
    initial_content = {
        "schema_version": 1,
        "catalog_version": "1.0.0",
        "log_aliases": {"test_alias": "sfvt_test.log"}
    }
    fake_catalog_path.write_text(json.dumps(initial_content), encoding="utf-8")

    # Monkeypatch 路径配置
    from app.routes import resolution_catalogs
    mock_allowed = {
        "resolution_catalog.json": {
            "title": "Test Catalog",
            "description": "Test",
            "path": fake_catalog_path
        }
    }
    monkeypatch.setattr(resolution_catalogs, "_ALLOWED_CATALOGS", mock_allowed)

    new_content = {
        "schema_version": 1,
        "catalog_version": "2.0.0",
        "log_aliases": {"new_alias": "sfvt_new.log"}
    }
    put_payload = {"content": json.dumps(new_content)}

    resp = client.put("/api/kb/resolution-catalogs/resolution_catalog.json", json=put_payload)
    assert resp.status_code == 200
    res = resp.json()
    assert res["success"] is True
    assert "保存成功" in res["message"]

    # 验证写回文件的格式
    written_text = fake_catalog_path.read_text(encoding="utf-8")
    assert "2.0.0" in written_text
    assert "sfvt_new.log" in written_text


def test_update_catalog_validation_failure_blocks_save(client: TestClient) -> None:
    """测试语法错误的 JSON 被拦截禁止保存并返回 400"""
    bad_payload = {"content": "INVALID JSON"}
    resp = client.put("/api/kb/resolution-catalogs/acli_command_catalog.json", json=bad_payload)
    assert resp.status_code == 400
    assert "无法保存" in resp.json()["detail"]
