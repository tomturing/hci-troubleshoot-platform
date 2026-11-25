"""acli 插件规格域层：平台服务端唯一事实源。

职责边界（对应用户需求与 2026-11-14 重新设计事件文档）：

* ``normalize_spec`` / ``validate_spec`` 把 admin-ui 提交的插件信息收敛成一份
  **不含任何运行环境信息**的规格：页面不知道 ``node_ip``、SSH 口令/私钥、
  bridge 地址与 case_id——本模块不感知任何目标环境。
* ``build_descriptor`` 由规格确定性生成 HCI 侧四要素（wrapper / 脚本 /
  manifest / 注册项）、逐文件 SHA-256 与 ``spec_hash``，作为阶段二 manifest 事实源。
* 设备侧部署脚本与自检解析已随链路收口删除（D1），由 terminal_bridge 阶段二
  用 Go 重新实现。回执**不接受前端上报的「成功」**，以设备侧 ACP_RESULT
  自检结论为准，避免重演 PR #1508 的可伪造回执。

24 环境实测约束（2026-11-13）：HCI 定制系统无 ``base64`` / ``python3``，
落盘只能用 shell 内建 ``printf '%b'``；注册表必须 ``jq`` 写临时文件后 ``mv``
原子替换；部署与 acli plugin install/uninstall/sync 共用 ``plugin_register.lock``。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

# 设备侧路径事实：PLUGIN_BASE/ACLI_BIN 参与文件路径与冒烟命令的确定性生成；
# 注册表/补全/临时目录等部署期路径已移交 terminal_bridge 阶段二 Go 实现持有。
PLUGIN_BASE = "/sf/data/local/acli/plugins"
ACLI_BIN = "/sf/bin/acli"

PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SCRIPT_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*$")
# smoke_args 会在设备侧以 $SMOKE_ARGS 展开成多个 argv，必须禁止任何 shell 元字符。
SMOKE_ARGS_RE = re.compile(r"^[A-Za-z0-9_.=:/@, -]*$")
SAFE_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

# 单文件内容上限，与桥侧 acliPluginMaxFileBytes
# （terminal_bridge/internal/acli/acli_plugin_manifest.go）同口径。
# 桥侧超限会整单拒收 manifest（manifest_file_too_large）；若平台侧不校验，
# 会出现「admin-ui 保存成功、客户侧永远下发失败」的误导（2026-11-24 现场）。
# 2026-11-25 按运维需求由 1MiB 放宽到 5MiB（完整巡检脚本已进入 ~1MiB 量级）。
MAX_PLUGIN_FILE_BYTES = 5 << 20
_MAX_PLUGIN_FILE_MIB = MAX_PLUGIN_FILE_BYTES // (1024 * 1024)

MANIFEST_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "name",
        "version",
        "source",
        "title",
        "vendor_name",
        "description",
        "change_log",
        "arch",
        "container",
        "hci_version",
        "acli_version",
        "dependencies",
        "hash_algo",
        "hash",
    }
)
REGISTER_REQUIRED_FIELDS = frozenset({"id", "name", "version", "status", "source", "built_in", "last_updated_at"})

# 页面提交体里出现这些键即视为契约破坏：运行环境不属于插件规格。
FORBIDDEN_SPEC_FIELDS = frozenset(
    {
        "node_ip",
        "device_host",
        "device_port",
        "device_username",
        "password",
        "passwd",
        "private_key",
        "auth_type",
        "ssh_port",
        "bridge_url",
        "case_id",
        "conversation_id",
        "exec_id",
        "trace_id",
        "credential_ref",
    }
)

ALLOWED_SPEC_FIELDS = frozenset(
    {
        "update_mode",
        "plugin_name",
        "plugin_uuid",
        "display_name",
        "title_zh",
        "title_en",
        "description_zh",
        "description_en",
        "vendor_name",
        "change_log",
        "version",
        "acli_version",
        "hci_version",
        "source",
        "status",
        "arch",
        "container",
        "interpreter",
        "script_filename",
        "script_content",
        "parameters",
        "extra_files",
        "enable_completion",
        "passthrough_help",
        "enable_pid_lock",
        "timeout_seconds",
        "smoke_args",
        "source_url",
    }
)

# 24 环境出厂内置插件（2026-11-13 实测）。覆盖更新只允许命中这张清单。
BUILTIN_PLUGINS = {
    "asan_ops": "1.0.0",
    "asys": "1.1.60",
    "log_collect": "1.0.0",
    "netdoctor": "1.0.0",
    "performance_tools": "1.0.9",
    "vm_suspend": "1.0.0",
    "vm_start": "1.0.0",
}

_DEFAULTS: dict[str, Any] = {
    "update_mode": "third_party",
    "plugin_name": "",
    "plugin_uuid": "",
    "display_name": "",
    "title_zh": "",
    "title_en": "",
    "description_zh": "",
    "description_en": "",
    "vendor_name": "HTP",
    "change_log": "",
    "version": "0.0.1",
    "acli_version": ">=1.0.0",
    "hci_version": "",
    "source": 3,
    "status": 1,
    "arch": 1,
    "container": "",
    "interpreter": "bash",
    "script_filename": "troubleshoot.sh",
    "script_content": "",
    "parameters": [],
    "extra_files": [],
    "enable_completion": True,
    "passthrough_help": False,
    "enable_pid_lock": False,
    "timeout_seconds": 120,
    "smoke_args": "",
    "source_url": "",
}

# 新增插件信息模板：与 docs/solution/admin-ui/acli插件新增信息收集模板.md §9 同源。
# 工具名口径是 lmt（小写字母 L），不是数字 1。
TEMPLATE_SPEC: dict[str, Any] = {
    "update_mode": "third_party",
    "plugin_name": "hci_lmt_check",
    "plugin_uuid": "",
    "display_name": "lmt 排障工具",
    "title_zh": "lmt 排障工具",
    "title_en": "lmt Check",
    "description_zh": "HCI 主机基础环境与服务只读巡检",
    "description_en": "Read-only host health inspection",
    "vendor_name": "HTP",
    "change_log": "首版：按使用说明补齐插件元数据",
    "version": "0.0.1",
    "acli_version": ">=1.0.0",
    "hci_version": "",
    "source": 3,
    "status": 1,
    "arch": 1,
    "container": "",
    "interpreter": "bash",
    "script_filename": "troubleshoot.sh",
    "script_content": "#!/bin/bash\n# 粘贴 lmt 原始脚本\n",
    "parameters": [{"flag": "-m", "description": "巡检模式", "required": True, "default": ""}],
    "extra_files": [],
    "enable_completion": True,
    "passthrough_help": False,
    "enable_pid_lock": False,
    "timeout_seconds": 120,
    "smoke_args": "-m env -c cpu_load",
    "source_url": "",
}


class SpecValidationError(ValueError):
    """插件规格不合法；``errors`` 是可直接回显给 admin-ui 的结构化原因。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _sha256(raw: str | bytes) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_spec(raw: Mapping[str, Any]) -> dict[str, Any]:
    """白名单收敛规格：拒绝运行环境字段、丢弃未知键、补齐默认值。

    未知键被丢弃而不是保存，是为了防止管理员误把主机/凭据信息带进
    ``acli_plugin_spec.spec_json`` 这一长期事实源。
    """
    if not isinstance(raw, Mapping):
        raise SpecValidationError(["请求体必须是对象"])

    forbidden = sorted(set(raw) & FORBIDDEN_SPEC_FIELDS)
    if forbidden:
        raise SpecValidationError([f"插件规格禁止包含运行环境字段: {', '.join(forbidden)}"])

    spec = dict(_DEFAULTS)
    for key in ALLOWED_SPEC_FIELDS:
        if key in raw and raw[key] is not None:
            spec[key] = raw[key]

    for key in ("parameters", "extra_files"):
        if not isinstance(spec[key], list):
            raise SpecValidationError([f"{key} 必须是数组"])

    spec["plugin_name"] = str(spec["plugin_name"]).strip()
    spec["script_filename"] = str(spec["script_filename"]).strip()
    spec["smoke_args"] = str(spec["smoke_args"]).strip()
    spec["update_mode"] = str(spec["update_mode"]).strip()

    if spec["update_mode"] == "builtin_override":
        spec["source"] = 1
    else:
        spec["source"] = int(spec["source"] or 3)

    for key in ("status", "arch", "timeout_seconds"):
        spec[key] = int(spec[key] or 0)

    for key in ("enable_completion", "passthrough_help", "enable_pid_lock"):
        spec[key] = bool(spec[key])

    # 覆盖内置插件必须沿用现场注册项原 id；平台自动生成的 uuid5 会破坏出厂关联，
    # 因此这里故意留空，交给 validate_spec 明确报错。
    if not spec["plugin_uuid"] and spec["update_mode"] != "builtin_override":
        spec["plugin_uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"htp-acli-plugin:{spec['plugin_name']}"))

    normalized_parameters = []
    for item in spec["parameters"]:
        if not isinstance(item, Mapping):
            raise SpecValidationError(["parameters 元素必须是对象"])
        normalized_parameters.append(
            {
                "flag": str(item.get("flag", "")).strip(),
                "description": str(item.get("description", "")).strip(),
                "required": bool(item.get("required", False)),
                "default": str(item.get("default", "") or ""),
            }
        )
    spec["parameters"] = normalized_parameters

    normalized_files = []
    for item in spec["extra_files"]:
        if not isinstance(item, Mapping):
            raise SpecValidationError(["extra_files 元素必须是对象"])
        normalized_files.append(
            {
                "path": str(item.get("path", "")).strip(),
                "mode": str(item.get("mode", "0644") or "0644"),
                "content": str(item.get("content", "") or ""),
            }
        )
    spec["extra_files"] = normalized_files
    return spec


def validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """校验 acli 硬性契约，返回可直接落 ``validation`` JSONB 的结构。"""
    errors: list[str] = []
    warnings: list[str] = []

    name = str(spec.get("plugin_name") or "")
    mode = str(spec.get("update_mode") or "")
    if not PLUGIN_NAME_RE.fullmatch(name):
        errors.append("plugin_name 必须匹配 ^[a-z][a-z0-9_]{0,63}$")
    if mode not in ("third_party", "builtin_override"):
        errors.append("update_mode 只能是 third_party 或 builtin_override")
    if mode == "builtin_override":
        if name not in BUILTIN_PLUGINS:
            errors.append("builtin_override 只允许覆盖出厂内置插件")
        if not spec.get("plugin_uuid"):
            errors.append("builtin_override 必须提供现场注册项的原 id（plugin_uuid）")
        if not str(spec.get("change_log") or "").strip():
            errors.append("builtin_override 必须填写 change_log")
        if int(spec.get("source") or 0) != 1:
            errors.append("builtin_override 的 source 必须为 1（官方包）")
    elif name in BUILTIN_PLUGINS:
        errors.append("新增插件不得与出厂内置插件重名；如需覆盖请显式选择 builtin_override")

    if not VERSION_RE.fullmatch(str(spec.get("version") or "")):
        errors.append("version 必须是语义化版本，如 0.0.1")
    if not SCRIPT_FILENAME_RE.fullmatch(str(spec.get("script_filename") or "")):
        errors.append("script_filename 只允许 [A-Za-z0-9._-]，且不能以 / 或 .. 开头")
    if not str(spec.get("script_content") or "").strip():
        errors.append("script_content 不能为空")
    if str(spec.get("interpreter") or "") not in ("bash", "sh"):
        errors.append("interpreter 只允许 bash 或 sh")

    smoke_args = str(spec.get("smoke_args") or "").strip()
    if not smoke_args:
        errors.append("smoke_args 必填：必须真实调用插件，不能只跑 --help")
    elif not SMOKE_ARGS_RE.fullmatch(smoke_args):
        errors.append("smoke_args 含 shell 元字符，禁止下发")
    elif smoke_args in ("--help", "-h", "help"):
        errors.append("smoke_args 不能只是 --help：必须产生真实巡检输出")

    timeout = int(spec.get("timeout_seconds") or 0)
    if not 1 <= timeout <= 300:
        errors.append("timeout_seconds 必须在 1~300 之间")
    if int(spec.get("status") or 0) != 1:
        errors.append("status 固定为 1（开启）")
    if int(spec.get("arch") or 0) != 1:
        errors.append("arch 固定为 1（x86）")
    if str(spec.get("container") or ""):
        errors.append("手工插件 container 必须为空（宿主机执行）")

    for item in spec.get("parameters") or []:
        if not FLAG_RE.fullmatch(str(item.get("flag") or "")):
            errors.append(f"参数 flag 非法: {item.get('flag')!r}")
    for item in spec.get("extra_files") or []:
        path = str(item.get("path") or "")
        if not path or not SAFE_RELATIVE_PATH_RE.fullmatch(path) or path.startswith("/") or ".." in path.split("/"):
            errors.append(f"附加文件路径非法: {path!r}")
        if str(item.get("mode") or "") not in ("0644", "0755"):
            errors.append(f"附加文件 mode 只允许 0644/0755: {path!r}")

    # 单文件大小：按 UTF-8 字节数校验（脚本常含中文注释，字符数会低估约 3 倍）。
    # 与桥侧 MAX_FILE_BYTES 同口径，避免保存成功、客户侧却整单拒收。
    script_bytes = len(str(spec.get("script_content") or "").encode("utf-8"))
    if script_bytes > MAX_PLUGIN_FILE_BYTES:
        errors.append(
            f"script_content 超过单文件上限 {_MAX_PLUGIN_FILE_MIB}MiB（当前 {script_bytes} 字节），请精简脚本后再保存"
        )
    for index, item in enumerate(spec.get("extra_files") or []):
        content_bytes = len(str(item.get("content") or "").encode("utf-8"))
        if content_bytes > MAX_PLUGIN_FILE_BYTES:
            errors.append(
                f"附加文件 extra_files[{index}] 超过单文件上限 {_MAX_PLUGIN_FILE_MIB}MiB（当前 {content_bytes} 字节）"
            )

    if not str(spec.get("title_zh") or "").strip():
        warnings.append("title_zh 为空，manifest 将回退使用插件名")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def build_wrapper(spec: Mapping[str, Any]) -> str:
    params = spec.get("parameters") or []
    name = str(spec["plugin_name"])
    filename = str(spec["script_filename"])
    interpreter = shlex.quote(str(spec["interpreter"]))
    lines = [
        "#!/bin/bash",
        "# GENERATED BY htp-acli-plugin-builder. DO NOT EDIT.",
        "# 插件入口：acli 把插件参数原样透传给 wrapper，再由它调用原始脚本。",
        "set -u",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        f'SCRIPT="$SCRIPT_DIR/bin/{filename}"',
        f"INTERPRETER={interpreter}",
        "print_help() {",
        "  cat <<'HELP'",
        f"用法: {ACLI_BIN} --timeout <sec> {name} [参数...]",
        f"说明: {spec.get('description_zh') or spec.get('display_name') or name}",
        "参数:",
    ]
    for item in params:
        required = "必填" if item.get("required") else "可选"
        default = item.get("default")
        suffix = "" if default in (None, "") else f"，默认 {default}"
        lines.append(f"  {item.get('flag', '')}  {item.get('description', '')} [{required}{suffix}]")
    lines += ["  -h|--help  显示本帮助", "HELP", "}", 'case "${1:-}" in']
    if spec.get("passthrough_help"):
        lines.append('  -h|--help|help) "$INTERPRETER" "$SCRIPT" "$@"; exit $? ;;')
    else:
        lines.append("  -h|--help|help) print_help; exit 0 ;;")
    lines += [
        "esac",
        '"$INTERPRETER" "$SCRIPT" "$@"',
        "rc=$?",
        "exit $rc",
        "",
    ]
    return "\n".join(lines)


def build_files(spec: Mapping[str, Any], wrapper: str) -> list[dict[str, str]]:
    """生成 HCI 侧插件目录的全部文件（manifest 由 hash 回填后再加入）。"""
    name = str(spec["plugin_name"])
    interpreter = str(spec["interpreter"])
    # 内置覆盖只替换 extra_files 声明的脚本：原 wrapper、bin 与其他出厂资产
    # 原样保留，目录结构与出厂完全一致（不写 stub、不创建空 bin 目录）。
    builtin = str(spec.get("update_mode") or "") == "builtin_override"
    files: list[dict[str, str]] = (
        []
        if builtin
        else [
            {"path": f"{name}/plugin", "mode": "0755", "content": wrapper},
            {
                "path": f"{name}/bin/{spec['script_filename']}",
                "mode": "0755" if interpreter == "bash" else "0644",
                "content": str(spec["script_content"]),
            },
        ]
    )
    for extra in spec.get("extra_files") or []:
        files.append(
            {
                "path": f"{name}/{extra['path']}",
                "mode": str(extra["mode"]),
                "content": str(extra["content"]),
            }
        )
    for item in files:
        item["sha256"] = _sha256(item["content"])
    return sorted(files, key=lambda item: item["path"])


def build_manifest(spec: Mapping[str, Any], files: list[dict[str, str]]) -> dict[str, Any]:
    name = str(spec["plugin_name"])
    hashes = {
        item["path"][len(name) + 1 :]: item["sha256"] for item in files if item["path"] != f"{name}/manifest.json"
    }
    vendor = str(spec.get("vendor_name") or "HTP")
    description_zh = str(spec.get("description_zh") or "")
    description_en = str(spec.get("description_en") or description_zh)
    title_zh = str(spec.get("title_zh") or name)
    title_en = str(spec.get("title_en") or title_zh)
    change_log = str(spec.get("change_log") or "")
    return {
        "id": str(spec["plugin_uuid"]),
        "name": name,
        "version": str(spec["version"]),
        "source": int(spec["source"]),
        "title": {"zh_cn": title_zh, "en_us": title_en},
        "vendor_name": {"zh_cn": vendor, "en_us": vendor},
        "description": {"zh_cn": description_zh, "en_us": description_en},
        "change_log": {"zh_cn": change_log, "en_us": change_log},
        "arch": int(spec["arch"]),
        "container": "",
        "hci_version": str(spec.get("hci_version") or ""),
        "acli_version": str(spec.get("acli_version") or ">=1.0.0"),
        "dependencies": {},
        "hash_algo": 1,
        "hash": hashes,
    }


def build_register_item(spec: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    stamp = now or datetime.now(timezone(timedelta(hours=8)))
    return {
        "id": str(spec["plugin_uuid"]),
        "name": str(spec["plugin_name"]),
        "version": str(spec["version"]),
        "status": 1,
        "source": int(spec["source"]),
        "built_in": spec["update_mode"] == "builtin_override",
        "last_updated_at": stamp.isoformat(),
    }


def build_descriptor(
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """由规格确定性生成 HCI 四要素与文件清单（manifest 事实源）。"""
    stamp = now or datetime.now(UTC)
    name = str(spec["plugin_name"])
    wrapper = build_wrapper(spec)
    files = build_files(spec, wrapper)
    manifest = build_manifest(spec, files)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    files.append(
        {
            "path": f"{name}/manifest.json",
            "mode": "0644",
            "content": manifest_text,
            "sha256": _sha256(manifest_text),
        }
    )
    files.sort(key=lambda item: item["path"])
    register_item = build_register_item(spec, now=stamp)
    spec_payload = {
        "spec": dict(spec),
        "files": {item["path"]: item["sha256"] for item in files},
        "register_item": {key: value for key, value in register_item.items() if key != "last_updated_at"},
    }
    spec_hash = _sha256(_canonical(spec_payload))

    smoke_args = str(spec["smoke_args"])
    descriptor: dict[str, Any] = {
        "descriptor_version": "1.0",
        "plugin_name": name,
        "version": str(spec["version"]),
        "spec_hash": spec_hash,
        "update_mode": str(spec["update_mode"]),
        "built_in": spec["update_mode"] == "builtin_override",
        "smoke_args": smoke_args,
        "enable_completion": bool(spec["enable_completion"]),
        "files": files,
        "manifest": manifest,
        "wrapper": wrapper,
        "register_item": register_item,
        "smoke_command": f"{ACLI_BIN} --timeout 60 {name} {smoke_args}",
        "verify_commands": [
            f"{ACLI_BIN} --formatter json plugin list",
            f"{ACLI_BIN} --timeout 60 {name} {smoke_args}",
        ],
    }
    return descriptor


def validate_descriptor(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """对 Builder 产物做交付前自检，防止把坏 manifest 发布出去。"""
    errors: list[str] = []
    manifest = descriptor.get("manifest") or {}
    missing = sorted(MANIFEST_REQUIRED_FIELDS - set(manifest))
    if missing:
        errors.append(f"manifest 缺字段: {', '.join(missing)}")
    register = descriptor.get("register_item") or {}
    missing_register = sorted(REGISTER_REQUIRED_FIELDS - set(register))
    if missing_register:
        errors.append(f"注册项缺字段: {', '.join(missing_register)}")

    files = descriptor.get("files") or []
    name = str(descriptor.get("plugin_name") or "")
    expected_hashes = {
        str(item["path"])[len(name) + 1 :]: item["sha256"]
        for item in files
        if str(item["path"]) != f"{name}/manifest.json"
    }
    if expected_hashes != manifest.get("hash"):
        errors.append("manifest.hash 与文件 SHA-256 不一致")

    if not descriptor.get("built_in"):
        wrapper = str(descriptor.get("wrapper") or "")
        if not wrapper.startswith("#!/bin/bash"):
            errors.append("wrapper 缺少 shebang")
        if '"$SCRIPT_DIR/bin/' not in wrapper:
            errors.append("wrapper 的 SCRIPT_DIR 必须原样展开，不能被单引号包住")
        if '"$@"' not in wrapper:
            errors.append('wrapper 必须原样透传 "$@"')
        plugin_file = next((item for item in files if item["path"] == f"{name}/plugin"), None)
        if not plugin_file or plugin_file["mode"] != "0755":
            errors.append(f"{name}/plugin 权限必须是 0755")

    return {"ok": not errors, "errors": errors}


def compile_descriptor(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """把 Builder 产物压成「可直接落盘」的固化载荷，随规格 revision 一起入库。

    为什么要固化而不是每次现算：``register_item.last_updated_at`` 取的是构建
    时刻。每次拉 manifest 都重算的话，期望态自身每秒都在变，节点侧「无修改 →
    Skip」永远判不出来，差量三态退化成全量重装。

    只保留阶段二真正要写的东西：
    * ``files`` —— 逐文件 path/mode/sha256/content（wrapper 与 manifest.json
      都已在清单内，不再单列，避免同一内容两处真相）；
    * ``register_item`` —— 设备侧 ``plugin_register.json`` 的期望条目；
    * 其余为比较/冒烟所需的标量。
    """
    files = [
        {
            "path": str(item["path"]),
            "mode": str(item["mode"]),
            "sha256": str(item["sha256"]),
            "content": str(item["content"]),
        }
        for item in descriptor.get("files") or []
    ]
    return {
        "version": str(descriptor["version"]),
        "spec_hash": str(descriptor["spec_hash"]),
        "update_mode": str(descriptor["update_mode"]),
        "built_in": bool(descriptor["built_in"]),
        "smoke_args": str(descriptor["smoke_args"]),
        "enable_completion": bool(descriptor["enable_completion"]),
        "register_item": dict(descriptor["register_item"]),
        "files": files,
        # 内联载荷总字节数：桥据此决定单次写入预算，平台据此核对超限规格。
        "size": sum(len(item["content"].encode("utf-8")) for item in files),
    }
