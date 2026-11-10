"""日志收集「自定义场景」的请求/响应模型。

三条边界，写在模型层而不是路由层：

1. **只允许字面绝对路径**。acli 的 config.paths 会逐条 `cp -r` 且未加引号
   （single_collect.py:388），空格/分号/`$()` 不是"收不到"，而是能在客户节点执行命令；
   通配符 `*` 会被 `os.path.exists` 判假后静默跳过，因此也必须拒绝。
2. **允许 `{date}` 占位**，但只允许这一个占位符。spc 清单里大量按天归档的文件，
   平台需要在创建任务时按用户选的时间范围展开成字面路径；接受任意花括号等于放开注入面。
3. **客户侧永不下发 items**。路径清单既是信息泄露面（客户的目录结构），
   也是注入面（前端可篡改）；客户只需要知道"这个场景有多少条路径"。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 与 bridge 的 logCollectPathRe 同源；`{date}` 先替换成单字符再校验。
# 注意这里刻意不允许 `*`：config 的 paths 不做 glob，`*` 只会静默跳过。
_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_./-]+$")
# spc 侧 copy 允许 * 与 {date}：插件自己做 glob 与占位替换，不经过 shell。
_COPY_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_./*-]+$")
_SCRIPT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_DATE_PLACEHOLDER = "{date}"

MAX_ITEMS = 200
MAX_PATH_LENGTH = 512
CODE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"

ScenarioSource = Literal["visual", "json", "spc"]
ScenarioAction = Literal["copy", "command", "script"]
ScenarioReviewState = Literal["pending_review", "approved"]
ScenarioCategory = Literal["log", "config", "file"]


class CustomScenarioItem(BaseModel):
    """一个收集条目。

    action 决定 path 的语义，校验必须跟着分叉：copy 允许通配与 {date}
    （spc 侧由插件处理），script 只能是脚本名，command 是节点上执行的命令、
    只允许管理端写入。
    """

    action: ScenarioAction = "copy"
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    category: ScenarioCategory = "log"
    note: str | None = Field(default=None, max_length=100)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    date_placeholders: list[dict[str, Any]] = Field(default_factory=list)
    docker: str | None = Field(default=None, max_length=32)
    interpreter: str | None = Field(default=None, max_length=64)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            raise ValueError("path 不能为空")
        if "\n" in raw or "\r" in raw or "\x00" in raw:
            raise ValueError("path 不能包含换行或空字符")
        return raw

    @model_validator(mode="after")
    def _validate_by_action(self):
        if self.action == "copy":
            if not self.path.startswith("/"):
                raise ValueError("copy 条目必须是绝对路径（以 / 开头）")
            if ".." in self.path:
                raise ValueError("路径不允许包含 ..（禁止路径穿越）")
            if "," in self.path:
                raise ValueError("路径不能包含逗号")
            probe = self.path.replace(_DATE_PLACEHOLDER, "D").replace("*", "D")
            if not _COPY_PATH_PATTERN.match(probe):
                raise ValueError("copy 路径只允许字母、数字、下划线、点、斜杠、短横线、* 与 {date}")
        elif self.action == "script":
            if not _SCRIPT_NAME_PATTERN.match(self.path):
                raise ValueError("script 只允许脚本文件名（字母、数字、下划线、点、短横线）")
        elif len(self.path) > 2000:
            raise ValueError("command 不能超过 2000 字符")
        return self


def _normalize_items(items: list[CustomScenarioItem] | None) -> list[CustomScenarioItem]:
    """去重并保持顺序。

    重复项不会让 acli 报错，但会让 `-p` 字符串变长、体积预估翻倍，
    用户会以为"同一条路径被收了两次"。
    """
    if not items:
        return []
    seen: set[str] = set()
    result: list[CustomScenarioItem] = []
    for item in items:
        if item.path in seen:
            continue
        seen.add(item.path)
        result.append(item)
    if len(result) > MAX_ITEMS:
        raise ValueError(f"路径条数不能超过 {MAX_ITEMS}")
    return result


class LogCollectCustomScenarioCreate(BaseModel):
    """新建自定义场景（管理端）。"""

    code: str = Field(pattern=CODE_PATTERN, max_length=32)
    label: str = Field(min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=200)
    applies_to: str | None = Field(default=None, max_length=200)
    items: list[CustomScenarioItem] = Field(default_factory=list)
    source: ScenarioSource = "visual"
    enabled: bool = True
    sort_order: int = 100
    review_state: ScenarioReviewState = "pending_review"

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("label 不能为空")
        return stripped

    @field_validator("summary", "applies_to")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("items")
    @classmethod
    def _dedupe_items(cls, value: list[CustomScenarioItem]) -> list[CustomScenarioItem]:
        return _normalize_items(value)


class LogCollectCustomScenarioUpdate(BaseModel):
    """修改场景：`code` 不可改（历史任务按 code+revision 回溯）。

    ``source`` 允许修改：它是**下发通道**而非录入方式，管理员需要能在
    「spc 覆写清单」与「-c 路径清单」之间纠错（例如误建了 spc 但节点无 spc 目录）。
    切换后 items 必须对新通道重新过 ``_validate_items_for_source``：
    带 filters/通配符的条目不能留在 -c 通道里，否则会被插件静默跳过。
    """

    label: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=200)
    applies_to: str | None = Field(default=None, max_length=200)
    items: list[CustomScenarioItem] | None = None
    source: ScenarioSource | None = None
    enabled: bool | None = None
    sort_order: int | None = None
    review_state: ScenarioReviewState | None = None

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("label 不能为空")
        return stripped

    @field_validator("items")
    @classmethod
    def _dedupe_items(cls, value: list[CustomScenarioItem] | None) -> list[CustomScenarioItem] | None:
        if value is None:
            return None
        return _normalize_items(value)


class LogCollectCustomScenarioResponse(BaseModel):
    """管理端视图：含完整 items 与下发内容摘要。"""

    model_config = ConfigDict(from_attributes=True)

    scenario_id: UUID
    revision: int
    code: str
    label: str
    summary: str | None = None
    applies_to: str | None = None
    items: list[CustomScenarioItem] = Field(default_factory=list)
    item_count: int = 0
    config_sha256: str | None = None
    config_bytes: int | None = None
    source: ScenarioSource = "visual"
    enabled: bool = True
    sort_order: int = 100
    review_state: ScenarioReviewState = "pending_review"
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LogCollectCustomScenarioListResponse(BaseModel):
    scenarios: list[LogCollectCustomScenarioResponse]


class LogCollectCustomScenarioClientItem(BaseModel):
    """客户侧视图：**只有元数据，没有 items**。

    客户需要判断"该不该选"，靠的是条数与体积，而不是路径清单；
    下发路径属于平台内部配置，前端既不该看到也不该能改。
    """

    scenario_id: UUID
    revision: int
    label: str
    summary: str | None = None
    item_count: int = 0


class LogCollectCustomScenarioClientListResponse(BaseModel):
    scenarios: list[LogCollectCustomScenarioClientItem]


class CustomScenarioValidationIssue(BaseModel):
    """校验结果的一条问题：带下标便于前端定位到具体勾选项。"""

    index: int | None = None
    field: str
    message: str


class CustomScenarioValidationResponse(BaseModel):
    """保存前试算结果。

    ``preview_text`` 是**服务端真实序列化产物**（``_serialize`` 的返回值），
    不是前端复刻：两条通道的下发格式完全不同（-c 是 ``{"paths":"a,b"}``，
    spc 是带 note 分组与 filters 的数组），前端各写一份必然漂移，
    管理员看到的预览就与实际下发的内容不一致。
    """

    ok: bool
    issues: list[CustomScenarioValidationIssue] = Field(default_factory=list)
    item_count: int = 0
    config_bytes: int = 0
    preview_kind: Literal["config", "spc"] = "config"
    preview_text: str = ""
