"""共享异常响应的序列化回归测试。"""

from typing import Self

from fastapi import FastAPI
from pydantic import BaseModel, model_validator
from shared.utils.exception_handlers import register_exception_handlers
from starlette.testclient import TestClient


class VersionSnapshotRequest(BaseModel):
    lock_version: int | None = None

    @model_validator(mode="after")
    def validate_lock_version(self) -> Self:
        if self.lock_version is None:
            raise ValueError("KBD lock_version 无效")
        return self


def test_model_validator_value_error_returns_serializable_422() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/snapshot")
    async def create_snapshot(_body: VersionSnapshotRequest) -> dict[str, bool]:
        return {"success": True}

    response = TestClient(app, raise_server_exceptions=False).post("/snapshot", json={})

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["msg"] == "Value error, KBD lock_version 无效"
    assert detail["ctx"]["error"] == "KBD lock_version 无效"
