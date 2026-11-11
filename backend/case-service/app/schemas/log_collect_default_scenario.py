"""默认场景清单覆写的数据结构。

与自定义场景共用 ``CustomScenarioItem`` 校验：路径白名单、动作枚举、条数上限
只在一处定义，避免两个编辑器各自放宽后出现「平台能存、桥会拒绝」的错配。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .log_collect_custom_scenario import CustomScenarioItem


class DefaultScenarioSave(BaseModel):
    """保存某个默认场景的收集条目（日志/配置文件）。"""

    items: list[CustomScenarioItem] = Field(default_factory=list)


class DefaultScenarioPreviewItem(BaseModel):
    """客户侧只读预览：只暴露路径与归类，不下发命令与过滤条件。"""

    path: str
    category: str = "log"
    note: str | None = None


class AdminDefaultScenarioManifest(BaseModel):
    """管理端清单：含可编辑的完整条目。"""

    module: str
    label: str
    customized: bool
    revision: int
    item_count: int
    sha256: str
    items: list[CustomScenarioItem] = Field(default_factory=list)


class ClientDefaultScenarioManifest(BaseModel):
    """客户侧清单：只读预览，不下发结构化条目。"""

    module: str
    label: str
    customized: bool
    revision: int
    item_count: int
    sha256: str
    preview: list[DefaultScenarioPreviewItem] = Field(default_factory=list)


class AdminDefaultScenarioListResponse(BaseModel):
    manifests: list[AdminDefaultScenarioManifest]
    sources: list[dict] = Field(default_factory=list)
    total_items: int = 0


class ClientDefaultScenarioListResponse(BaseModel):
    manifests: list[ClientDefaultScenarioManifest]


class DefaultScenarioValidationResponse(BaseModel):
    """保存前试算：编辑器据此回答「这份清单最终下发长什么样」。"""

    ok: bool
    item_count: int = 0
    sha256: str = ""
    bytes: int = 0
    preview_text: str = ""
    issues: list[str] = Field(default_factory=list)
