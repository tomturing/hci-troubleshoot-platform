"""Diagnosis Service（诊断服务）分片直传 CORS（跨来源资源共享）测试。"""

import pytest
from app.config import Settings
from app.main import app
from fastapi.testclient import TestClient


def preflight(origin: str):
    """发送浏览器上传分片前的 CORS 预检。"""

    return TestClient(app).options(
        "/api/direct/diagnosis-uploads/00000000-0000-0000-0000-000000000001/parts/1",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type,x-upload-token,x-part-sha256",
        },
    )


def test_direct_upload_preflight_allows_configured_customer_origin():
    """本地 Customer UI（客户界面）必须能够通过分片上传预检。"""

    response = preflight("http://localhost:3001")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3001"
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "x-upload-token" in allowed_headers
    assert "x-part-sha256" in allowed_headers


def test_direct_upload_preflight_rejects_unconfigured_origin():
    """未配置来源即使知道上传路径也不能获得浏览器 CORS 授权。"""

    response = preflight("https://untrusted.example")

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_diagnosis_cors_origins_rejects_wildcard():
    """携带上传能力令牌的数据面不得使用通配 Origin（来源）。"""

    configured = Settings(DIAGNOSIS_ALLOWED_ORIGINS="*")

    with pytest.raises(ValueError, match="禁止使用通配符"):
        _ = configured.diagnosis_cors_origins
