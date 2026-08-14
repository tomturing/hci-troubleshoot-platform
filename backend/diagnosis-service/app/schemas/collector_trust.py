"""Collector 离线信任链 API 契约。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CollectorTrustStoreResponse(BaseModel):
    """当前 Collector 受信根响应。"""

    schema_version: str
    generated_at: datetime
    keys: list[dict[str, Any]]


class CollectorRevocationListResponse(BaseModel):
    """租户范围签名吊销清单响应。"""

    schema_version: str
    generated_at: datetime
    next_update_at: datetime
    revoked_artifacts: list[dict[str, Any]]
    document_signature: dict[str, Any]
