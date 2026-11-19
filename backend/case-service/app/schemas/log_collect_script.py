"""日志收集「收集脚本」库的请求/响应模型。

边界与默认场景一致：内容校验的唯一真源在 service 层（同时被保存、导入、
场景引用校验、任务下发四处复用），模型层只做长度与类型约束，避免多份规则漂移。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScriptSource = Literal["manual", "import", "baseline"]


class LogCollectScriptSaveRequest(BaseModel):
    """保存新版本（覆写内置脚本 / 平台新增脚本继续迭代）。"""

    content: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=64)
    interpreter: str | None = Field(default=None, max_length=16)
    change_note: str | None = Field(default=None, max_length=200)


class LogCollectScriptCreateRequest(LogCollectScriptSaveRequest):
    """新建平台脚本；``code`` 是落盘文件名，创建后不可改。"""

    code: str = Field(min_length=1, max_length=64)
    source: Literal["manual", "import"] = "manual"


class LogCollectScriptValidateRequest(BaseModel):
    """保存前试算：返回归一化后的真实下发内容摘要。"""

    code: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)
    interpreter: str | None = Field(default=None, max_length=16)


class LogCollectScriptSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    interpreter: str
    content_bytes: int
    content_sha256: str
    revision: int = 0
    customized: bool = False
    source: str = "baseline"
    enabled: bool = True
    binary: bool = False
    ref_usable: bool = True
    in_baseline: bool = True
    updated_at: str | None = None
    change_note: str | None = None


class LogCollectScriptListResponse(BaseModel):
    scripts: list[LogCollectScriptSummary]
    total: int = 0
    baseline_total: int = 0
    # demo（仓库示例）/ env（环境注入）/ missing：UI 要能让管理员知道
    # 当前列表的"内置脚本"是不是真实设备清单，避免把示例当成节点事实。
    baseline_source: str = "demo"


class LogCollectScriptDetail(LogCollectScriptSummary):
    content: str | None = None


class LogCollectScriptRevision(BaseModel):
    code: str
    revision: int
    name: str
    interpreter: str
    content_sha256: str
    content_bytes: int
    change_note: str | None = None
    source: str = "manual"
    enabled: bool = True
    created_by: str | None = None
    created_at: str | None = None


class LogCollectScriptRevisionDetail(LogCollectScriptRevision):
    content: str


class LogCollectScriptRevisionListResponse(BaseModel):
    revisions: list[LogCollectScriptRevision]


class LogCollectScriptValidationResponse(BaseModel):
    ok: bool
    code: str = ""
    interpreter: str = ""
    content_sha256: str = ""
    content_bytes: int = 0
    issues: list[str] = Field(default_factory=list)


class LogCollectScriptReference(BaseModel):
    kind: Literal["default", "custom"]
    target: str
    revision: int


class LogCollectScriptReferenceListResponse(BaseModel):
    references: list[LogCollectScriptReference]
