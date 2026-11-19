"""日志收集「收集脚本」库服务。

第一性原理与默认场景完全同构：

- ``data/script_catalog/`` 是随镜像发布的**平台默认快照**（从 HCI 插件
  acli 日志收集插件的脚本目录），只读，运行期不变；
- 数据库 ``log_collect_script`` 只存**被改过的脚本**与**平台新增脚本**，
  恢复默认 = 删除该 code 的全部行，退回快照；
- 任务下发时只下发"被覆写过/平台新增"的脚本：未改动的内置脚本本来就在节点上，
  下发它只会制造无意义的写盘与还原风险（与默认场景"未覆写即零行为变化"一致）。

与默认场景的唯一差别是版本：脚本必须保留每一版原文。默认场景单主键只留最新版，
脚本用 ``(script_id, revision)``，历史任务才能回放"当时节点上跑的是哪一版脚本"。

安全边界（服务端与 bridge 双侧校验，缺一不可）：

1. ``code`` 是节点上的文件路径最后一段，只允许单层文件名，禁止 ``/`` 与 ``..``；
2. 顶层文件才能被场景引用（spc 的 ``action=script`` 的 path 不允许斜杠）；
3. 单文件字节上限 4MB（基线里最大的 ``vs_script/tierd.check`` 为 1.84MB）；
4. 只接受 UTF-8 文本；二进制基线（ELF）只展示、不可编辑、不可下发；
5. 内容禁止 NUL，CRLF 归一化为 LF（保证 sha256 与节点写入内容稳定）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.log_collect import (
    LogCollectCustomScenario,
    LogCollectDefaultScenario,
    LogCollectScript,
)

logger = logging.getLogger(__name__)

# 真实基线清单由环境注入（生产环境指向设备探测产出的清单）；
# 仓库内只保留 demo 摘要，避免把厂商脚本清单与指纹固化进公开仓库。
BASELINE_ENV = "LOG_COLLECT_SCRIPT_BASELINE_INDEX"

# 基线只保存元数据（文件名/解释器/摘要/大小），不保存脚本原文。
#
# 为什么：本仓库是公开仓库，而 acli 插件的 script 目录是厂商专有代码
# （随插件版本分发，含厂商标识与内部实现）。平台只需要知道"节点上应该有什么"，
# 内容在编辑时由管理员导入，绝不把厂商源码复制进仓库/镜像。
BASELINE_INDEX = Path(__file__).resolve().parent.parent / "data" / "script_catalog" / "index.json"

# 1.84MB 的 tierd.check 已接近上限；4MB 留出编辑余量，同时挡住误传大文件。
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_NAME_LENGTH = 64
MAX_NOTE_LENGTH = 200

# spc 的 script path 只允许这一段；服务端必须先按同一口径收窄。
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

INTERPRETERS = {"python", "bash"}


class ScriptError(ValueError):
    """脚本内容、文件名或状态不合法。"""

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        # kind 只用于路由映射 HTTP 状态，不进入平台错误码目录（那是 bridge 运行时错误）。
        self.kind = kind


@dataclass(frozen=True)
class BaselineScript:
    """一份随镜像发布的脚本快照。"""

    code: str
    name: str
    interpreter: str
    content_bytes: int
    sha256: str
    binary: bool
    # 只有顶层文本文件才能被 spc 的 action=script 引用。
    ref_usable: bool


def sha256_of(data: str | bytes) -> str:
    """内容摘要的唯一实现：保存、试算、下发证据三处必须同源。"""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


# 内部沿用短名，避免几十处调用点噪声。
_sha256 = sha256_of


def infer_interpreter(code: str, content: str | None = None) -> str:
    """按后缀优先、shebang 兜底推断解释器。

    后缀优先是有意为之：管理员把 ``.py`` 内容粘进 ``.sh`` 条目时必须报错，
    而不是被 shebang 静默覆盖成 python。
    """
    lowered = code.lower()
    if lowered.endswith(".sh") or lowered.endswith(".bash"):
        return "bash"
    if lowered.endswith(".py"):
        return "python"
    if content:
        first = content.split("\n", 1)[0]
        if first.startswith("#!"):
            if "python" in first:
                return "python"
            if "bash" in first or "/sh" in first:
                return "bash"
    return "bash"


@lru_cache(maxsize=1)
def _load_baseline() -> tuple[dict[str, BaselineScript], str]:
    """读基线元数据索引，返回 (code -> 元数据, 来源)。

    来源优先级：环境变量 ``LOG_COLLECT_SCRIPT_BASELINE_INDEX`` → 仓库内 demo 清单。
    为什么允许环境覆盖：真实脚本清单与摘要属于设备侧事实，随 HCI 版本变化，
    既不该固化进公开仓库，也不该随镜像发布（不同设备版本内容不同）。
    子目录条目（``tools/...``）只展示、不可被场景引用、不可覆写。
    """
    scripts: dict[str, BaselineScript] = {}
    override = os.environ.get(BASELINE_ENV, "").strip()
    path = Path(override) if override else BASELINE_INDEX
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("log_collect.script_baseline_missing file=%s", path)
        return scripts, "missing"
    for item in payload.get("scripts", []):
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        scripts[code] = BaselineScript(
            code=code,
            name=str(item.get("name") or code),
            interpreter=str(item.get("interpreter") or infer_interpreter(code, None)),
            content_bytes=int(item.get("bytes") or 0),
            sha256=str(item.get("sha256") or ""),
            binary=bool(item.get("binary")),
            ref_usable=bool(item.get("ref_usable")),
        )
    return scripts, str(payload.get("kind") or ("env" if override else "demo"))


def baseline_scripts() -> dict[str, BaselineScript]:
    return _load_baseline()[0]


def baseline_source() -> str:
    """基线来源：demo（仓库示例）/ env（环境注入）/ missing（两者都没有）。"""
    return _load_baseline()[1]


def baseline_content(code: str) -> str | None:
    """内置脚本原文不进入平台：始终返回 None。

    管理端编辑内置脚本时必须由管理员提供内容（导入/粘贴），
    这也保证"节点跟随平台下发"的前提是平台真的下发过内容。
    """
    return None


def normalize_content(content: str) -> str:
    """归一化换行并做最小安全校验。"""
    if content is None:
        raise ScriptError("脚本内容不能为空")
    raw = content.replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        raise ScriptError("脚本内容不能为空")
    if "\x00" in raw:
        raise ScriptError("脚本内容不能包含空字符")
    size = len(raw.encode("utf-8"))
    if size > MAX_SCRIPT_BYTES:
        raise ScriptError(f"脚本过大：{size} 字节（上限 {MAX_SCRIPT_BYTES}）")
    return raw


def validate_code(code: str) -> str:
    raw = (code or "").strip()
    if not raw:
        raise ScriptError("脚本文件名不能为空")
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ScriptError("脚本文件名只能是单层文件名，不允许斜杠或 ..")
    if not CODE_RE.match(raw):
        raise ScriptError("脚本文件名只允许字母、数字、下划线、点、短横线，且不能以符号开头")
    return raw


class LogCollectScriptService:
    """脚本库读写。所有写操作只落数据库，节点文件由 bridge 在下发时写入。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- 读取 ----------

    async def _latest_rows(self) -> dict[str, LogCollectScript]:
        result = await self.session.execute(select(LogCollectScript))
        rows = list(result.scalars().all())
        latest: dict[str, LogCollectScript] = {}
        for row in rows:
            current = latest.get(row.code)
            if current is None or int(row.revision) > int(current.revision):
                latest[row.code] = row
        return latest

    async def list_scripts(self, *, full: bool = False) -> list[dict[str, Any]]:
        """基线快照 + 平台覆写/新增合并后的列表。

        ``customized=False`` 的行内容来自基线；``customized=True`` 的行内容来自库。
        """
        latest = await self._latest_rows()
        baseline = baseline_scripts()
        items: list[dict[str, Any]] = []

        def row_view(code: str, *, customized: bool, revision: int, source: str, content: str | None) -> dict[str, Any]:
            meta = baseline.get(code)
            name = meta.name if meta else code
            interpreter = meta.interpreter if meta else infer_interpreter(code, content)
            size = len(content.encode("utf-8")) if content is not None else (meta.content_bytes if meta else 0)
            sha = _sha256(content) if content is not None else (meta.sha256 if meta else "")
            binary = meta.binary if meta else False
            return {
                "code": code,
                "name": name,
                "interpreter": interpreter,
                "content_bytes": size,
                "content_sha256": sha,
                "revision": revision,
                "customized": customized,
                "source": source,
                "enabled": True,
                "binary": binary,
                "ref_usable": (meta.ref_usable if meta else ("/" not in code and not binary)),
                "in_baseline": meta is not None,
                "updated_at": None,
            }

        for code in baseline:
            override = latest.get(code)
            if override is None:
                items.append(row_view(code, customized=False, revision=0, source="baseline", content=None))
                continue
            view = row_view(
                code, customized=True, revision=int(override.revision), source=override.source, content=override.content
            )
            view["enabled"] = bool(override.enabled)
            view["name"] = override.name or view["name"]
            view["interpreter"] = override.interpreter
            view["updated_at"] = override.created_at.isoformat() if override.created_at else None
            view["change_note"] = override.change_note
            items.append(view)

        for code, row in latest.items():
            if code in baseline:
                continue
            view = row_view(code, customized=True, revision=int(row.revision), source=row.source, content=row.content)
            view["enabled"] = bool(row.enabled)
            view["name"] = row.name
            view["interpreter"] = row.interpreter
            view["updated_at"] = row.created_at.isoformat() if row.created_at else None
            view["change_note"] = row.change_note
            items.append(view)

        items.sort(key=lambda item: (0 if item["ref_usable"] else 1, item["code"]))
        return items

    async def content_of(self, code: str, revision: int | None = None) -> str | None:
        """取指定版本内容；revision=None 取库中最新版，库中没有则回退基线。"""
        if revision is not None:
            result = await self.session.execute(
                select(LogCollectScript)
                .where(LogCollectScript.code == code, LogCollectScript.revision == revision)
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return row.content
            # 子目录条目没有数据库行，直接回退基线正文。
            return baseline_content(code) if revision == 0 else None
        latest = await self._latest_rows()
        row = latest.get(code)
        if row is not None:
            return row.content
        return baseline_content(code)

    async def _script_id(self, code: str) -> uuid.UUID:
        result = await self.session.execute(
            select(LogCollectScript.script_id).where(LogCollectScript.code == code).limit(1)
        )
        script_id = result.scalar_one_or_none()
        if script_id is None:
            raise ScriptError(f"脚本不存在：{code}", "not_found")
        return script_id

    async def list_revisions(self, code: str) -> list[dict[str, Any]]:
        """该 code 的全部平台版本（基线不算版本，界面上单列为「平台默认」）。"""
        result = await self.session.execute(
            select(LogCollectScript).where(LogCollectScript.code == code).order_by(LogCollectScript.revision.desc())
        )
        rows = list(result.scalars().all())
        return [
            {
                "code": row.code,
                "revision": int(row.revision),
                "name": row.name,
                "interpreter": row.interpreter,
                "content_sha256": row.content_sha256,
                "content_bytes": int(row.content_bytes),
                "change_note": row.change_note,
                "source": row.source,
                "enabled": bool(row.enabled),
                "created_by": row.created_by,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    async def get_revision(self, code: str, revision: int) -> dict[str, Any] | None:
        script_id = await self._script_id(code)
        row = await self.session.get(LogCollectScript, (script_id, revision))
        if row is None:
            return None
        return {
            "code": row.code,
            "revision": int(row.revision),
            "name": row.name,
            "interpreter": row.interpreter,
            "content": row.content,
            "content_sha256": row.content_sha256,
            "content_bytes": int(row.content_bytes),
            "change_note": row.change_note,
            "source": row.source,
            "enabled": bool(row.enabled),
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    # ---------- 写入 ----------

    async def create(
        self,
        *,
        code: str,
        content: str,
        name: str | None,
        interpreter: str | None,
        change_note: str | None,
        source: str,
        actor: str | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        """新建平台脚本。基线里已有的 code 必须走 ``save``（语义是覆写内置脚本）。"""
        normal_code = validate_code(code)
        if normal_code in baseline_scripts():
            raise ScriptError("该文件名已存在于平台默认快照，请直接编辑（覆写）它，不要新建", "conflict")
        latest = await self._latest_rows()
        if normal_code in latest:
            raise ScriptError(f"脚本已存在：{normal_code}", "conflict")
        return await self._insert(
            normal_code,
            content=content,
            name=name,
            interpreter=interpreter,
            change_note=change_note or "初始版本",
            source=source,
            actor=actor,
            trace_id=trace_id,
        )

    async def save(
        self,
        *,
        code: str,
        content: str,
        name: str | None,
        interpreter: str | None,
        change_note: str | None,
        actor: str | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        """保存新版本：基线脚本 = 覆写，平台新增脚本 = 继续迭代。"""
        normal_code = validate_code(code)
        meta = baseline_scripts().get(normal_code)
        if meta is not None and not meta.ref_usable:
            raise ScriptError("该文件是二进制或子目录附件，不支持在线编辑", "invalid")
        if not change_note or not change_note.strip():
            raise ScriptError("请填写本次变更说明", "invalid")
        return await self._insert(
            normal_code,
            content=content,
            name=name,
            interpreter=interpreter,
            change_note=change_note.strip()[:MAX_NOTE_LENGTH],
            source="baseline" if meta is not None else "manual",
            actor=actor,
            trace_id=trace_id,
        )

    async def _insert(
        self,
        code: str,
        *,
        content: str,
        name: str | None,
        interpreter: str | None,
        change_note: str | None,
        source: str,
        actor: str | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        normalized = normalize_content(content)
        guessed = infer_interpreter(code, normalized)
        normalized_interp = (interpreter or guessed).strip().lower()
        if normalized_interp not in INTERPRETERS:
            raise ScriptError("解释器只支持 python / bash", "invalid")
        if normalized_interp != guessed:
            # 后缀与解释器不一致必然让 acli 用错解释器执行；直接拒绝而不是猜。
            raise ScriptError(f"{code} 应使用 {guessed} 解释器（与文件后缀/shebang 不一致）", "invalid")
        latest = await self._latest_rows()
        current = latest.get(code)
        if current is None:
            script_id = uuid.uuid4()
            revision = 1
        else:
            script_id = current.script_id
            revision = int(current.revision) + 1
        row = LogCollectScript(
            script_id=script_id,
            revision=revision,
            code=code,
            name=(name or code).strip()[:MAX_NAME_LENGTH] or code,
            interpreter=normalized_interp,
            content=normalized,
            content_sha256=_sha256(normalized),
            content_bytes=len(normalized.encode("utf-8")),
            change_note=change_note,
            source=source,
            enabled=True,
            trace_id=trace_id,
            created_by=(actor or "system")[:255],
        )
        self.session.add(row)
        await self.session.commit()
        return {
            "code": code,
            "revision": revision,
            "content_sha256": row.content_sha256,
            "content_bytes": int(row.content_bytes),
            "interpreter": row.interpreter,
            "name": row.name,
            "change_note": row.change_note,
        }

    async def reset(self, code: str) -> bool:
        """恢复默认：删除该 code 的全部覆写行，退回平台快照。"""
        meta = baseline_scripts().get(code)
        if meta is None:
            raise ScriptError("只有平台默认快照里的脚本才能恢复默认；新增脚本请直接删除", "invalid")
        result = await self.session.execute(delete(LogCollectScript).where(LogCollectScript.code == code))
        await self.session.commit()
        return bool(result.rowcount)

    async def delete_script(self, code: str) -> bool:
        """删除平台新增脚本。内置脚本不允许删除（只能恢复默认）。"""
        if code in baseline_scripts():
            raise ScriptError("内置脚本不能删除，请使用「恢复默认」", "invalid")
        result = await self.session.execute(delete(LogCollectScript).where(LogCollectScript.code == code))
        await self.session.commit()
        return bool(result.rowcount)

    async def set_enabled(self, code: str, enabled: bool) -> bool:
        if code not in baseline_scripts() and not (await self._latest_rows()).get(code):
            raise ScriptError(f"脚本不存在：{code}", "not_found")
        await self.session.execute(
            update(LogCollectScript).where(LogCollectScript.code == code).values(enabled=enabled)
        )
        await self.session.commit()
        return True

    # ---------- 引用与下发 ----------

    async def references(self, code: str) -> list[dict[str, Any]]:
        """反查引用：默认场景覆写行 + 各自定义场景的最新版本。"""
        refs: list[dict[str, Any]] = []

        default_rows = (await self.session.execute(select(LogCollectDefaultScenario))).scalars().all()
        for row in default_rows:
            if _items_reference(row.items, code):
                refs.append({"kind": "default", "target": row.module_code, "revision": int(row.revision)})

        custom_rows = (await self.session.execute(select(LogCollectCustomScenario))).scalars().all()
        latest_custom: dict[str, LogCollectCustomScenario] = {}
        for row in custom_rows:
            current = latest_custom.get(row.code)
            if current is None or int(row.revision) > int(current.revision):
                latest_custom[row.code] = row
        for row in latest_custom.values():
            if _items_reference(row.items, code):
                refs.append({"kind": "custom", "target": row.code, "revision": int(row.revision)})
        return refs

    async def assert_scenario_items(self, items: Iterable[Any]) -> list[str]:
        """场景保存前校验 action=script 的条目。

        返回"未命中脚本库"的自由输入路径（合法，引用节点自带脚本），
        命中脚本库但已停用的直接报错。
        """
        latest = await self._latest_rows()
        baseline = baseline_scripts()
        free_inputs: list[str] = []
        for item in items:
            action = getattr(item, "action", None) or (item.get("action") if isinstance(item, dict) else None)
            path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else None)
            if action != "script" or not path:
                continue
            row = latest.get(str(path))
            if row is not None and not row.enabled:
                raise ScriptError(f"脚本已停用：{path}，请在收集脚本页启用或改选其它脚本", "conflict")
            if row is None and str(path) not in baseline:
                free_inputs.append(str(path))
            # 解释器缺省时回填库/基线值：spc 的 interpreter 空着会让 acli 按后缀猜，
            # dpcollect 这类无扩展名脚本会被猜错，采集直接失败。
            if isinstance(item, dict) and not str(item.get("interpreter") or "").strip():
                interpreter = str(getattr(row, "interpreter", "") or "")
                if not interpreter and str(path) in baseline:
                    interpreter = baseline[str(path)].interpreter
                if interpreter:
                    item["interpreter"] = interpreter
        return free_inputs

    async def build_script_files(self, spc_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按本次 spc 内容生成需要下发的脚本文件。

        规则（与默认场景同构）：
        - 命中库中最新版本（被覆写的内置脚本 / 平台新增脚本）→ 下发；
        - 未覆写的内置脚本 → **不下发**（节点上本来就是原件，零行为变化）；
        - 未命中库 → 用户自由输入的路径，原样交给 acli，不下发；
        - 命中但已停用 → 抛错，由调用方挡住任务创建。
        """
        paths: list[str] = []
        for spc in spc_files or []:
            try:
                parsed = json.loads(str(spc.get("content") or "[]"))
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if not isinstance(entry, dict) or entry.get("action") != "script":
                    continue
                path = str(entry.get("path") or "").strip()
                if path and path not in paths:
                    paths.append(path)
        if not paths:
            return []
        latest = await self._latest_rows()
        files: list[dict[str, Any]] = []
        for path in paths:
            row = latest.get(path)
            if row is None:
                continue
            if not row.enabled:
                raise ScriptError(f"脚本已停用：{path}，无法下发", "conflict")
            files.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "content": row.content,
                    "sha256": row.content_sha256,
                    "bytes": int(row.content_bytes),
                    "revision": int(row.revision),
                    "interpreter": row.interpreter,
                }
            )
        return files


def _items_reference(items: Any, code: str) -> bool:
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict) and item.get("action") == "script" and str(item.get("path") or "") == code:
            return True
    return False
