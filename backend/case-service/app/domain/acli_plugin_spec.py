"""acli 插件规格域层：平台服务端唯一事实源。

职责边界（对应用户需求与 2026-11-14 重新设计事件文档）：

* ``normalize_spec`` / ``validate_spec`` 把 admin-ui 提交的插件信息收敛成一份
  **不含任何运行环境信息**的规格：页面不知道 ``node_ip``、SSH 口令/私钥、
  bridge 地址与 case_id，这些由服务端环境表与 Secret 在下发时注入。
* ``build_descriptor`` 由规格确定性生成 HCI 侧四要素（wrapper / 脚本 /
  manifest / 注册项）、逐文件 SHA-256、原子部署脚本与 ``spec_hash``。
* ``parse_deploy_result`` 从终端输出解析设备侧自检结论，服务端据此落库；
  **不接受前端上报的「成功」**，避免重演 PR #1508 的可伪造回执。

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

PLUGIN_BASE = "/sf/data/local/acli/plugins"
REGISTER_PATH = f"{PLUGIN_BASE}/plugin_register.json"
COMPLETION_PATH = "/sf/cfg/acli/etc/completion/bash_completion.conf"
TMP_BASE = "/sf/data/local/acli/tmp"
ACLI_BIN = "/sf/bin/acli"

PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SCRIPT_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*$")
# smoke_args 会在设备侧以 $SMOKE_ARGS 展开成多个 argv，必须禁止任何 shell 元字符。
SMOKE_ARGS_RE = re.compile(r"^[A-Za-z0-9_.=:/@, -]*$")
SAFE_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

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


def _printf_arg(text: str) -> str:
    """把任意文本编码成 ``printf '%b'`` 的单个安全 argv。

    ``%b`` 会解释反斜杠转义，因此先转义反斜杠本身与常见控制字符；
    单引号与其余 shell 元字符交给 ``shlex.quote``。
    """
    escaped = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return shlex.quote(escaped)


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


def build_deploy_script(descriptor: Mapping[str, Any]) -> str:
    """生成在 HCI 上执行的一次性原子部署脚本（含备份/校验/回滚/真实冒烟）。"""
    name = str(descriptor["plugin_name"])
    deploy_id = str(descriptor["deploy_id"])
    spec_hash = str(descriptor["spec_hash"])
    version = str(descriptor["version"])
    smoke_args = str(descriptor["smoke_args"])
    tmp = f"{TMP_BASE}/{deploy_id}"
    q = shlex.quote
    builtin = bool(descriptor["built_in"])
    lines = [
        "#!/bin/bash",
        f"# HTP acli plugin deploy {deploy_id} — generated by case-service",
        "set -u",
        f"BASE={q(PLUGIN_BASE)}",
        f"REG={q(REGISTER_PATH)}",
        f"COMPL={q(COMPLETION_PATH)}",
        f"TMP={q(tmp)}",
        f"NAME={q(name)}",
        f"DEPLOY_ID={q(deploy_id)}",
        f"VERSION={q(version)}",
        f"SPEC_HASH={q(spec_hash)}",
        f"ENABLE_COMPLETION={q('1' if descriptor['enable_completion'] else '0')}",
        f"SMOKE_ARGS={q(smoke_args)}",
        f"BUILTIN={q('1' if builtin else '0')}",
        "STATUS=failed",
        "ERROR=",
        "rollback() {",
        '  [ -f "$TMP/backup/plugin_register.json" ] && cp -a "$TMP/backup/plugin_register.json" "$REG"',
        '  [ -f "$TMP/backup/bash_completion.conf" ] && cp -a "$TMP/backup/bash_completion.conf" "$COMPL"',
        # 目录删除以"有备份 或 有 .old"为前提：lock_timeout / register_backup_failed
        # 都发生在目录备份之前，此时 "$BASE/$NAME" 是现场唯一副本，删了就再也回不来。
        '  if [ -d "$TMP/backup/$NAME" ] || [ -d "$BASE/$NAME.old.$DEPLOY_ID" ]; then',
        '    if [ -d "$BASE/$NAME" ]; then rm -rf "$BASE/$NAME"; fi',
        '    if [ -d "$TMP/backup/$NAME" ]; then cp -a "$TMP/backup/$NAME" "$BASE/$NAME"; fi',
        '    if [ -d "$BASE/$NAME.old.$DEPLOY_ID" ]; then mv "$BASE/$NAME.old.$DEPLOY_ID" "$BASE/$NAME"; fi',
        "  else",
        '    echo "ACP_ROLLBACK_SKIPPED=no_backup" >&2',
        "  fi",
        "  return 0",
        "}",
        "finish() {",
        '  echo "ACP_RESULT_BEGIN"',
        '  printf \'{"status":"%s","plugin_name":"%s","version":"%s","spec_hash":"%s","deploy_id":"%s","error":"%s"}\\n\' "$STATUS" "$NAME" "$VERSION" "$SPEC_HASH" "$DEPLOY_ID" "$ERROR"',
        '  echo "ACP_RESULT_END"',
        '  if [ -f "$TMP/plugin_list.json" ]; then',
        '    echo "ACP_PLUGIN_LIST_BEGIN"',
        '    cat "$TMP/plugin_list.json"',
        '    echo "ACP_PLUGIN_LIST_END"',
        "  fi",
        '  if [ -f "$TMP/smoke.out" ]; then',
        '    echo "ACP_SMOKE_BEGIN"',
        '    cat "$TMP/smoke.out"',
        '    echo "ACP_SMOKE_END"',
        "  fi",
        "  exit 0",
        "}",
        "fail() {",
        "  trap - INT TERM HUP",
        '  ERROR="$1"',
        '  echo "ACP_ERROR=$1" >&2',
        "  rollback",
        '  STATUS="rolled_back"',
        "  finish",
        "}",
        # 脚本被 SIGTERM/SIGKILL 打断时停在"旧目录已挪走、新目录未就位"之间；
        # INT/TERM/HUP 走 fail 收敛到回滚，SIGKILL 由下次执行前的自愈逻辑兜底。
        "trap 'fail interrupted' INT TERM HUP",
        (
            'mkdir -p "$TMP/backup" "$TMP/stage/$NAME" || { echo "ACP_ERROR=mkdir_failed" >&2; exit 1; }'
            if builtin
            else 'mkdir -p "$TMP/backup" "$TMP/stage/$NAME/bin" || { echo "ACP_ERROR=mkdir_failed" >&2; exit 1; }'
        ),
        'exec 9>"$BASE/plugin_register.lock" || fail "lock_open_failed"',
        'flock -w 30 9 || fail "lock_timeout"',
        'cp -a "$REG" "$TMP/backup/plugin_register.json" || fail \'register_backup_failed\'',
        '[ -f "$COMPL" ] && cp -a "$COMPL" "$TMP/backup/bash_completion.conf"',
        # 覆盖内置插件时原目录必须存在且必须备份成功，否则拒绝改动。
        'if [ "$BUILTIN" = "1" ]; then',
        "  [ -d \"$BASE/$NAME\" ] || fail 'builtin_plugin_missing'",
        '  cp -a "$BASE/$NAME" "$TMP/backup/" || fail \'builtin_backup_failed\'',
        "else",
        '  if [ -d "$BASE/$NAME" ]; then cp -a "$BASE/$NAME" "$TMP/backup/" || fail \'plugin_backup_failed\'; fi',
        "fi",
    ]
    # 覆盖内置插件：从原目录复制完整文件树（含二进制 .mo），只覆盖 spec 明确变更的
    # 脚本文件，其余文件保持不变；随后由设备侧重算 manifest.hash 保证一致。
    lines += [
        'if [ "$BUILTIN" = "1" ]; then',
        '  cp -a "$BASE/$NAME/." "$TMP/stage/$NAME/" || fail \'builtin_stage_copy_failed\'',
        "fi",
    ]
    for item in descriptor["files"]:
        rel = str(item["path"])[len(name) + 1 :]
        if builtin and rel == "plugin":
            continue
        lines += [
            f'mkdir -p "$(dirname "$TMP/stage/$NAME/{rel}")" || fail \'stage_mkdir_failed\'',
            f"printf '%b' {_printf_arg(str(item['content']))} > \"$TMP/stage/$NAME/{rel}\" || fail 'file_write_failed'",
            f"printf '%s  %s\\n' {q(str(item['sha256']))} \"$TMP/stage/$NAME/{rel}\" | sha256sum -c - >/dev/null || fail 'hash_mismatch'",
        ]
    if builtin:
        lines += [
            "cd \"$TMP/stage/$NAME\" || fail 'stage_cd_failed'",
            "HASH_JSON=$(find . -type f ! -name manifest.json -print0 | sort -z | while IFS= read -r -d '' f; do",
            "  sum=$(sha256sum \"$f\" | cut -d' ' -f1); rel=${f#./};",
            '  printf \'"%s":"%s",\' "$rel" "$sum";',
            "done)",
            'HASH_JSON="{${HASH_JSON%,}}"',
            "jq --argjson h \"$HASH_JSON\" '.hash = $h' manifest.json > manifest.new || fail 'manifest_hash_rebuild_failed'",
            "mv manifest.new manifest.json || fail 'manifest_replace_failed'",
            "cd - >/dev/null || true",
        ]
    lines += [
        "chmod 0755 \"$TMP/stage/$NAME/plugin\" || fail 'chmod_failed'",
        f"printf '%b' {_printf_arg(_canonical(descriptor['register_item']))} > \"$TMP/register_item.json\" || fail 'register_item_write_failed'",
        'if [ -d "$BASE/$NAME" ]; then mv "$BASE/$NAME" "$BASE/$NAME.old.$DEPLOY_ID" || fail "old_dir_move_failed"; fi',
        'mv "$TMP/stage/$NAME" "$BASE/$NAME" || fail "stage_move_failed"',
        'jq --slurpfile item "$TMP/register_item.json" \'(. // []) | del(.[] | select(.name == $item[0].name)) + [$item[0]]\' "$REG" > "$TMP/plugin_register.json" || fail "jq_register_failed"',
        'jq -e \'type == "array"\' "$TMP/plugin_register.json" >/dev/null || fail "register_json_invalid"',
        'mv "$TMP/plugin_register.json" "$REG" || fail "register_replace_failed"',
        'if [ "$ENABLE_COMPLETION" = "1" ] && [ -f "$COMPL" ]; then grep -qx "$NAME" "$COMPL" || printf "\\n%s\\n" "$NAME" >> "$COMPL"; fi',
        f"{ACLI_BIN} --formatter json plugin list > \"$TMP/plugin_list.json\" 2>&1 || fail 'plugin_list_failed'",
        'grep -q "\\"Name\\": \\"$NAME\\"" "$TMP/plugin_list.json" || fail "plugin_not_registered"',
        f'{ACLI_BIN} --timeout 60 "$NAME" --help > "$TMP/help.out" 2>&1 || fail \'plugin_help_failed\'',
        f'{ACLI_BIN} --timeout 60 "$NAME" $SMOKE_ARGS > "$TMP/smoke.out" 2>&1 || fail "plugin_smoke_failed"',
        '[ -s "$TMP/smoke.out" ] || fail "plugin_smoke_empty"',
        'rm -rf "$BASE/$NAME.old.$DEPLOY_ID"',
        'STATUS="success"',
        "finish",
        "",
    ]
    return "\n".join(lines)


def build_descriptor(
    spec: Mapping[str, Any],
    *,
    deploy_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """由规格确定性生成 HCI 四要素与可直接下发的命令。"""
    stamp = now or datetime.now(UTC)
    name = str(spec["plugin_name"])
    did = deploy_id or f"htp-acli-{stamp.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
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
        "deploy_id": did,
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
    script = build_deploy_script(descriptor)
    script_path = f"{TMP_BASE}/{did}.sh"
    descriptor["deploy_script"] = script
    descriptor["deploy_script_path"] = script_path
    # 大脚本（如 netdoctor 近 1MB）不能塞进单条 shell argv：Linux 单参数上限
    # 通常 128KB。按 48KB 分片写入再执行，规避 MAX_ARG_STRLEN。
    chunk_size = 48 * 1024
    chunks = [script[i : i + chunk_size] for i in range(0, len(script), chunk_size)] or [""]
    # 每条命令只写一个分片，避免把整份脚本拼成一条超长 WS 消息 / SSH 命令
    # （实测 1MB 命令在 bridge exec 通道会卡住且无 exec_result）。
    descriptor["deploy_commands"] = [
        f"printf '%b' {_printf_arg(chunk)} {'>' if index == 0 else '>>'} {script_path}"
        for index, chunk in enumerate(chunks)
    ] + [f"bash {script_path}; rc=$?; rm -f {script_path}; exit $rc"]
    # 兼容旧调用方：小脚本仍可用单条命令。
    descriptor["deploy_command"] = " && ".join(descriptor["deploy_commands"][:-1]) + (
        f" && {descriptor['deploy_commands'][-1]}"
    )
    return descriptor


def validate_descriptor(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """对 Builder 产物做交付前自检，防止把坏 manifest/脚本下发到 HCI。"""
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

    script = str(descriptor.get("deploy_script") or "")
    for token in ("base64", "python3"):
        if token in script:
            errors.append(f"部署脚本含 HCI 不存在的依赖: {token}")
    for token in (
        "sha256sum -c -",
        "jq --slurpfile",
        'mv "$TMP/plugin_register.json"',
        "flock -w 30",
        "ACP_SMOKE_BEGIN",
    ):
        if token not in script:
            errors.append(f"部署脚本缺少必要环节: {token}")
    smoke_invocation = f'{ACLI_BIN} --timeout 60 "$NAME" $SMOKE_ARGS'
    if not str(descriptor.get("smoke_args") or "") or smoke_invocation not in script:
        errors.append("部署脚本未包含真实冒烟调用")
    if register.get("built_in") is True and "builtin_backup_failed" not in script:
        errors.append("内置插件覆盖缺少强制备份")
    return {"ok": not errors, "errors": errors}


def _extract_block(stdout: str, begin: str, end: str) -> str:
    if begin not in stdout or end not in stdout:
        return ""
    return stdout.split(begin, 1)[1].split(end, 1)[0].strip()


def parse_deploy_result(stdout: str, *, exit_code: int) -> dict[str, Any]:
    """从设备侧输出解析自检结论；只有 success + 退出码 0 + 非空冒烟输出才算生效。"""
    raw = _extract_block(stdout or "", "ACP_RESULT_BEGIN", "ACP_RESULT_END")
    result: dict[str, Any] = {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            result = parsed
    except (ValueError, TypeError):
        result = {}

    smoke_output = _extract_block(stdout or "", "ACP_SMOKE_BEGIN", "ACP_SMOKE_END")
    status = str(result.get("status") or "")
    verification = {
        "device_status": status,
        "device_error": str(result.get("error") or ""),
        "device_spec_hash": str(result.get("spec_hash") or ""),
        "plugin_list": _extract_block(stdout or "", "ACP_PLUGIN_LIST_BEGIN", "ACP_PLUGIN_LIST_END"),
        "smoke_output": smoke_output,
        "exit_code": exit_code,
        "ok": status == "success" and exit_code == 0 and bool(smoke_output),
    }
    return verification
