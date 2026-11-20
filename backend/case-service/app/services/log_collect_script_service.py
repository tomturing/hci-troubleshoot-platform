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

# code 是相对插件 script/ 目录的路径，服务端必须先按同一口径收窄。
# 为什么允许子目录：插件自带脚本并不全在顶层——内置采集脚本就在 vs_script/ 等
# 子目录里，只认单层文件名等于「永远改不了这些脚本」。总长仍限 64（与库表列一致），
# 段数限 3，且每段都必须以字母/数字开头，从而天然排除绝对路径、空段与 ".."。
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*){0,2}$")
MAX_CODE_LENGTH = 64
MAX_CODE_DEPTH = 3

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


def shebang_interpreter(content: str | None) -> str | None:
    """取 shebang 声明的解释器；没有 shebang 返回 None。

    为什么 shebang 优先于后缀：节点上真正决定"谁来执行"的是解释器参数，而 shebang
    是脚本自己声明的执行方式。插件目录里大量脚本没有后缀（dpcollect、dpdoctor、
    lmt_collect_info），只认后缀会让它们要么导不进来、要么被猜成 bash 跑错。
    """
    if not content:
        return None
    first = content.split("\n", 1)[0].strip()
    if not first.startswith("#!"):
        return None
    lowered = first.lower()
    if "python" in lowered:
        return "python"
    if "bash" in lowered or lowered.endswith("/sh") or " sh" in lowered:
        return "bash"
    return None


def suffix_interpreter(code: str) -> str | None:
    """按后缀判断；无后缀或未知后缀返回 None。"""
    lowered = code.lower()
    if lowered.endswith(".sh") or lowered.endswith(".bash"):
        return "bash"
    if lowered.endswith(".py"):
        return "python"
    return None


def detect_interpreter(code: str, content: str | None = None) -> tuple[str, str]:
    """返回 (解释器, 判定依据)：shebang → suffix → default(bash)。

    后缀与 shebang 冲突时以 shebang 为准，由调用方提示而不是拒绝：拒绝会让
    "内容是对的、名字带错后缀"的文件完全没法入库，而 shebang 才是执行事实。
    """
    from_shebang = shebang_interpreter(content)
    if from_shebang:
        return from_shebang, "shebang"
    from_suffix = suffix_interpreter(code)
    if from_suffix:
        return from_suffix, "suffix"
    return "bash", "default"


def infer_interpreter(code: str, content: str | None = None) -> str:
    """兼容入口：只取判定结果，不关心依据。"""
    return detect_interpreter(code, content)[0]


def resolve_interpreter(
    code: str,
    content: str | None,
    explicit: str | None = None,
) -> tuple[str, str, list[str]]:
    """确定最终下发的解释器，返回 (解释器, 依据, 提示)。

    手选值优先（管理端显式声明就是最终事实），但不静默吞掉矛盾：
    与 shebang/后缀不一致时给提示，让"为什么节点按 X 跑"在界面上可解释。
    """
    detected, source = detect_interpreter(code, content)
    chosen = (explicit or "").strip().lower() or detected
    if chosen not in INTERPRETERS:
        raise ScriptError("解释器只支持 python / bash", "invalid")
    warnings: list[str] = []
    from_shebang = shebang_interpreter(content)
    if from_shebang and chosen != from_shebang:
        warnings.append(f"已按手选使用 {chosen}，与 shebang 声明的 {from_shebang} 不一致，节点会按 {chosen} 执行")
    from_suffix = suffix_interpreter(code)
    if not explicit and from_shebang and from_suffix and from_shebang != from_suffix:
        warnings.append(f"文件后缀指向 {from_suffix}，但 shebang 是 {from_shebang}，已按 shebang 使用 {from_shebang}")
    if not explicit and source == "default":
        warnings.append("无后缀且无 shebang，默认按 bash 执行；若内容是 Python 请手动选择解释器")
    return chosen, (source if not explicit else "manual"), warnings


@lru_cache(maxsize=1)
def _load_baseline() -> tuple[dict[str, BaselineScript], str]:
    """读基线元数据索引，返回 (code -> 元数据, 来源)。

    来源优先级：环境变量 ``LOG_COLLECT_SCRIPT_BASELINE_INDEX`` → 仓库内 demo 清单。
    为什么允许环境覆盖：真实脚本清单与摘要属于设备侧事实，随 HCI 版本变化，
    既不该固化进公开仓库，也不该随镜像发布（不同设备版本内容不同）。
    子目录条目（``vs_script/...``）同样可被引用与覆写：code 就是相对 ``script/``
    的路径。是否可引用由清单里的 ``ref_usable`` 决定（二进制/附件为 false）。
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
    """校验脚本相对路径（可含最多两层子目录）。

    拒绝项都是"拼进 shell 命令后能改路径语义"的形态：绝对路径、``..``、反斜杠、
    空段、尾随斜杠。允许子目录是因为插件内置脚本本来就住在 ``vs_script/`` 等子目录，
    平台要覆写它们就必须能表达同一路径。
    """
    raw = (code or "").strip()
    if not raw:
        raise ScriptError("脚本文件名不能为空")
    if len(raw) > MAX_CODE_LENGTH:
        raise ScriptError(f"脚本路径过长：{len(raw)}（上限 {MAX_CODE_LENGTH} 个字符）")
    if raw.startswith("/") or raw.endswith("/"):
        raise ScriptError("脚本路径必须是相对路径：不能以 / 开头或结尾")
    if "\\" in raw:
        raise ScriptError("脚本路径不允许反斜杠，请用 / 分隔目录")
    if ".." in raw:
        raise ScriptError("脚本路径不允许 .. 或含连续点号的目录穿越")
    if not CODE_RE.match(raw):
        raise ScriptError("脚本路径只允许字母、数字、下划线、点、短横线，用 / 分隔目录（最多三层）")
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
        # 后缀只作为弱证据：真正的执行方式由 shebang 或管理员手选决定。
        # 旧实现把"后缀 != 推断值"当成硬错误，导致无后缀脚本与非 .py/.sh 命名
        # 的内置脚本既导不进来也存不下去。
        normalized_interp, _, _ = resolve_interpreter(code, normalized, interpreter)
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

        没有任何 action=script 条目时直接返回、**不查库**：绝大多数场景只配
        路径与专项，为它们多开一次 SELECT 既无意义，也让「保存场景」在库不可用
        时被连坐失败。
        """
        script_items: list[tuple[Any, str]] = []
        for item in items:
            action = getattr(item, "action", None) or (item.get("action") if isinstance(item, dict) else None)
            path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else None)
            if action != "script" or not path:
                continue
            script_items.append((item, str(path)))
        if not script_items:
            return []

        latest = await self._latest_rows()
        baseline = baseline_scripts()
        free_inputs: list[str] = []
        for item, path in script_items:
            row = latest.get(path)
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
