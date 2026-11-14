"""acli 插件管理路由 — admin-ui「acli插件」页面的配置、产物生成与下发回执。

链路：admin-ui → API 网关(/api/v1/tools/{path} 泛向代理) → 本路由（配置事实源）
      → 前端通过 terminal_bridge 的 ssh_exec_process 隔离通道执行 deploy_command
      → 客户 HCI 落盘插件四要素并注册生效 → exec_result → 本路由记录回执。

HCI 侧契约来自 24 环境实测（/sf/data/local/acli/plugins/plugin_register.json、
asan_ops/manifest.json）与《acli 手工插件（方案 B）Web 化部署与巡检平台技术规格说明书》。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from shared.database.postgres import DatabaseManager  # noqa: F401
from shared.observability.logger import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acli_plugin import AcliPlugin, AcliPluginDeployment
from app.routes import tool_definition as tool_routes

logger = get_logger(__name__)

# 挂在网关已有的 /api/v1/tools/{path:path} 泛向代理下：新增前端能力无需改 api-gateway。
router = APIRouter(prefix="/api/v1/tools/acli-plugins", tags=["acli-plugins"])

PLUGIN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PLUGIN_BASE = "/sf/data/local/acli/plugins"
REGISTER_PATH = f"{PLUGIN_BASE}/plugin_register.json"
COMPLETION_PATH = "/sf/cfg/acli/etc/completion/bash_completion.conf"
TMP_BASE = "/sf/data/local/acli/tmp"
ACLI_BIN = "/sf/bin/acli"

# 24 环境实测（2026-11-13）：7 个出厂内置插件及字段契约，供页面作为配置参考。
REFERENCE_PLUGINS = [
    {"name": "asan_ops", "version": "1.0.0", "source": 1, "built_in": True, "container": "vs-cp-manager"},
    {"name": "asys", "version": "1.1.60", "source": 1, "built_in": True, "container": ""},
    {"name": "log_collect", "version": "1.0.0", "source": 1, "built_in": True, "container": ""},
    {"name": "netdoctor", "version": "1.0.0", "source": 1, "built_in": True, "container": ""},
    {"name": "performance_tools", "version": "1.0.9", "source": 1, "built_in": True, "container": ""},
    {"name": "vm_suspend", "version": "1.0.0", "source": 1, "built_in": True, "container": ""},
    {"name": "vm_start", "version": "1.0.0", "source": 1, "built_in": True, "container": ""},
]

RUNTIME_CONTRACT = {
    "source": "24 环境实测 2026-11-13",
    "paths": {
        "plugin_base": PLUGIN_BASE,
        "register": REGISTER_PATH,
        "completion": COMPLETION_PATH,
        "tmp": TMP_BASE,
        "acli_bin": ACLI_BIN,
        "factory_readonly": "/sf/etc/acli/plugins/plugins.json",
        "init_watermark_readonly": f"{PLUGIN_BASE}/last_built_in_plugins_init.json",
    },
    "register_item_required": ["id", "name", "version", "status", "source", "built_in", "last_updated_at"],
    "manifest_required": [
        "id", "name", "version", "source", "title", "vendor_name", "description", "change_log",
        "arch", "container", "hci_version", "acli_version", "dependencies", "hash_algo", "hash",
    ],
    "enums": {
        "status": {"1": "开启（平台固定）"},
        "source": {"1": "官方包", "2": "运维包", "3": "第三方包（平台默认）"},
        "arch": {"1": "x86"},
        "hash_algo": {"1": "SHA-256"},
    },
    "hard_rules": [
        "禁止调用 acli plugin install（.aclix 需厂商签名私钥，且 /support/tmp 路径门禁）",
        "plugin 必须 0755、带 shebang、原样透传 \"$@\"",
        "manifest.hash 覆盖除 manifest.json 自身外的全部文件，值为小写 SHA-256",
        "注册表必须 jq 生成 tmp 后 mv 原子替换，禁止直接覆写",
        "built_in 必须为 false；不修改出厂清单与初始化水位",
        "部署与 acli plugin install/uninstall/sync 共用 plugin_register.lock，需 flock 互斥",
    ],
    "observed_environment": {"host": "host-047bcb4bc820", "acli_version": "1.0.0", "jq": True, "flock": True, "sha256sum": True, "python": False},
}

# 参考 support.sangfor.com.cn《[HCI-V1] 1mt 排障工具使用方法》实现的原始巡检脚本。
# 语法：bash troubleshoot.sh -m <模式> -s <检查模块> -c <检查项> -i <虚拟机ID> -d -vv
ONE_MT_SCRIPT = r'''#!/bin/bash
# 1mt 排障工具 —— HCI 主机基础环境与服务巡检（HTP acli 插件模板）
# 参考：support.sangfor.com.cn 故障案例《[HCI-V1] 1mt 排障工具使用方法》
# 容器内/宿主机均可执行；所有检查均为只读命令。
set -u
MODE="env"
MODULE="all"
CHECK="all"
VM_ID=""
DEBUG=0
VERBOSE=0

usage() {
  cat <<'USAGE'
用法: troubleshoot.sh -m <模式> -s <检查模块> -c <检查项> -i <虚拟机ID> -d -vv
  -m  模式: env(环境检查, 默认) | service(服务检查)
  -s  检查模块: all | cpu_load | disk_partition | memory | network
  -c  检查项: all | cpu_load | disk_partition | memory | network | service
  -i  虚拟机 ID（保留参数，当前检查不依赖）
  -d  输出调试信息
  -vv 输出详细命令原文
示例: bash troubleshoot.sh -m env -c all
      bash troubleshoot.sh -m env -c cpu_load -vv
      bash troubleshoot.sh -m service -c service
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    -m) MODE="${2:-}"; shift 2 ;;
    -s) MODULE="${2:-}"; shift 2 ;;
    -c) CHECK="${2:-}"; shift 2 ;;
    -i) VM_ID="${2:-}"; shift 2 ;;
    -d) DEBUG=1; shift ;;
    -vv) VERBOSE=1; shift ;;
    -h|--help|help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

section() { printf '\n===== [%s] %s =====\n' "$1" "$2"; }
run_cmd() {
  [ "$VERBOSE" = "1" ] && printf '$ %s\n' "$1"
  eval "$1" 2>&1
  return 0
}
check_cpu_load() {
  section env cpu_load
  run_cmd "cat /proc/loadavg"
  run_cmd "uptime"
}
check_disk_partition() {
  section env disk_partition
  run_cmd "df -h"
}
check_memory() {
  section env memory
  run_cmd "free -m"
}
check_network() {
  section env network
  run_cmd "ip -s link 2>/dev/null | head -60"
  run_cmd "cat /proc/net/dev"
}
check_service() {
  section service service
  if command -v systemctl >/dev/null 2>&1; then
    run_cmd "systemctl --failed --no-pager 2>/dev/null | head -40"
    run_cmd "systemctl is-active sshd 2>/dev/null"
  else
    echo "systemctl 不可用，跳过服务状态检查"
  fi
}

printf '1mt 排障工具 | mode=%s module=%s check=%s vm_id=%s\n' "$MODE" "$MODULE" "$CHECK" "$VM_ID"
printf '时间: %s | 主机: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$(hostname)"

want="$CHECK"
[ "$want" = "all" ] && [ "$MODULE" != "all" ] && want="$MODULE"
case "$MODE" in
  env)
    case "$want" in
      all) check_cpu_load; check_disk_partition; check_memory; check_network ;;
      cpu_load) check_cpu_load ;;
      disk_partition) check_disk_partition ;;
      memory) check_memory ;;
      network) check_network ;;
      *) echo "未知检查项: $want" >&2; exit 2 ;;
    esac
    ;;
  service)
    check_service
    ;;
  *)
    echo "未知模式: $MODE" >&2; usage; exit 2 ;;
esac
[ "$DEBUG" = "1" ] && printf '\n[debug] mode=%s module=%s check=%s vm_id=%s\n' "$MODE" "$MODULE" "$CHECK" "$VM_ID"
printf '\n巡检完成（只读检查，未修改任何配置）\n'
exit 0
'''

ONE_MT_PARAMETERS = [
    {"flag": "-m", "name": "mode", "type": "string", "required": False, "default": "env", "description": "模式：env 环境检查 / service 服务检查"},
    {"flag": "-s", "name": "module", "type": "string", "required": False, "default": "all", "description": "检查模块"},
    {"flag": "-c", "name": "check", "type": "string", "required": False, "default": "all", "description": "检查项：all / cpu_load / disk_partition / memory / network / service"},
    {"flag": "-i", "name": "vm_id", "type": "string", "required": False, "default": "", "description": "虚拟机 ID"},
    {"flag": "-d", "name": "debug", "type": "bool", "required": False, "default": False, "description": "调试输出"},
    {"flag": "-vv", "name": "verbose", "type": "bool", "required": False, "default": False, "description": "详细输出"},
]

ONE_MT_TEMPLATE = {
    "plugin_name": "hci_1mt_check",
    "display_name": "1mt排障工具",
    "title_zh": "HCI 1mt 排障工具",
    "title_en": "HCI 1mt Troubleshooting Tool",
    "description_zh": "参考深信服技术支持《[HCI-V1] 1mt 排障工具使用方法》，对 HCI 主机执行环境与服务只读巡检（cpu_load / disk_partition / memory / network / service）。",
    "description_en": "Read-only HCI host health inspection based on Sangfor 1mt troubleshooting guide.",
    "vendor_name": "HTP",
    "change_log": "v0.0.1 基于 support 文档与 24 环境实测契约创建",
    "version": "0.0.1",
    "acli_version": ">=1.0.0",
    "hci_version": "",
    "source": 3,
    "status": 1,
    "arch": 1,
    "container": "",
    "interpreter": "bash",
    "script_filename": "troubleshoot.sh",
    "script_content": ONE_MT_SCRIPT,
    "parameters": ONE_MT_PARAMETERS,
    "extra_files": [],
    "enable_completion": True,
    "passthrough_help": False,
    "enable_pid_lock": False,
    "timeout_seconds": 120,
    "smoke_args": "-m env -c cpu_load",
    "device_host": "172.28.24.1",
    "device_port": 22,
    "device_username": "root",
    "node_ip": "172.28.24.1",
    "source_url": "https://support.sangfor.com.cn/cases/list?product_id=33&type=1&category_id=36753",
    "is_active": True,
}

EDITABLE_FIELDS = [
    "plugin_name", "display_name", "title_zh", "title_en", "description_zh", "description_en",
    "vendor_name", "change_log", "version", "acli_version", "hci_version", "source", "status",
    "arch", "container", "interpreter", "script_filename", "script_content", "parameters",
    "extra_files", "enable_completion", "passthrough_help", "enable_pid_lock", "timeout_seconds",
    "smoke_args",
    "device_host", "device_port", "device_username", "node_ip", "source_url", "is_active",
]


async def get_db() -> AsyncSession:
    """复用 tool_definition 路由已注入的 DatabaseManager（lifespan 中统一初始化）。"""
    manager: DatabaseManager | None = tool_routes.database_manager
    if not manager:
        raise HTTPException(status_code=500, detail="数据库管理器未初始化")
    async for session in manager.get_session():
        yield session


def _sha256(raw: str | bytes) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def _printf_arg(text: str) -> str:
    """把任意文本编码为 shell 单引号参数，配合 `printf '%b'` 写文件。

    HCI 定制系统没有 base64 / python3，故不依赖任何外部解码器：
    反斜杠加倍、换行转 \\n、单引号用 '\\'' 转义，命令保持单行。
    """
    escaped = text.replace("\\", "\\\\").replace("\n", "\\n")
    return "'" + escaped.replace("'", "'\\''") + "'"


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("plugin_name") or "").strip()
    if not PLUGIN_NAME_PATTERN.match(name):
        raise HTTPException(status_code=400, detail="插件名必须为小写字母开头，仅含小写字母/数字/下划线，长度 1-64")
    script = data.get("script_content")
    if not isinstance(script, str) or not script.strip():
        raise HTTPException(status_code=400, detail="原始脚本内容不能为空")
    filename = str(data.get("script_filename") or "troubleshoot.sh")
    if not re.match(r"^[A-Za-z0-9._-]{1,255}$", filename):
        raise HTTPException(status_code=400, detail="脚本文件名只允许字母、数字、点、下划线、连字符")
    for extra in data.get("extra_files") or []:
        path = str(extra.get("path") or "")
        if not path or path.startswith("/") or ".." in path or path.startswith("manifest.json"):
            raise HTTPException(status_code=400, detail=f"附加文件路径非法: {path}")
    return data


def _apply(plugin: AcliPlugin, data: dict[str, Any]) -> None:
    for field in EDITABLE_FIELDS:
        if field in data:
            setattr(plugin, field, data[field])
    if not plugin.plugin_uuid:
        plugin.plugin_uuid = str(uuid.uuid4())


def _build_wrapper(p: dict[str, Any]) -> str:
    params = p.get("parameters") or []
    name = p["plugin_name"]
    filename = p.get("script_filename") or "troubleshoot.sh"
    interpreter = p.get("interpreter") or "bash"
    lines = [
        "#!/bin/bash",
        "# GENERATED BY htp-acli-plugin-builder. DO NOT EDIT.",
        "# 插件入口：acli 把插件参数原样透传给本 wrapper，再由它调用原始脚本。",
        "set -u",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        f"SCRIPT=\"$SCRIPT_DIR/bin/{filename}\"",
        f"INTERPRETER={shlex.quote(interpreter)}",
        "print_help() {",
        "  cat <<'HELP'",
        f"用法: /sf/bin/acli --timeout <sec> {name} [参数...]",
        f"说明: {p.get('description_zh') or p.get('display_name') or name}",
        "参数:",
    ]
    for item in params:
        required = "必填" if item.get("required") else "可选"
        default = item.get("default")
        suffix = "" if default in (None, "") else f"，默认 {default}"
        lines.append(f"  {item.get('flag', '')}  {item.get('description', '')} [{required}{suffix}]")
    lines += [
        "  -h|--help  显示本帮助",
        "HELP",
        "}",
        'case "${1:-}" in',
    ]
    if p.get("passthrough_help"):
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


def _build_files(p: dict[str, Any], wrapper: str) -> list[dict[str, str]]:
    """生成 HCI 侧插件目录的全部文件（manifest 由 hash 回填后再加入）。"""
    name = p["plugin_name"]
    files: list[dict[str, str]] = [
        {"path": f"{name}/plugin", "mode": "0755", "content": wrapper},
        {
            "path": f"{name}/bin/{p.get('script_filename') or 'troubleshoot.sh'}",
            "mode": "0755" if (p.get("interpreter") or "bash") == "bash" else "0644",
            "content": p["script_content"],
        },
    ]
    for extra in p.get("extra_files") or []:
        files.append({
            "path": f"{name}/{extra['path']}",
            "mode": str(extra.get("mode") or "0644"),
            "content": str(extra.get("content") or ""),
        })
    for item in files:
        item["sha256"] = _sha256(item["content"])
    return files


def _build_manifest(p: dict[str, Any], files: list[dict[str, str]]) -> dict[str, Any]:
    name = p["plugin_name"]
    hashes = {
        item["path"][len(name) + 1:]: item["sha256"]
        for item in sorted(files, key=lambda x: x["path"])
    }
    return {
        "id": p["plugin_uuid"],
        "name": name,
        "version": p.get("version") or "0.0.1",
        "source": int(p.get("source") or 3),
        "title": {"zh_cn": p.get("title_zh") or name, "en_us": p.get("title_en") or name},
        "vendor_name": {"zh_cn": p.get("vendor_name") or "HTP", "en_us": p.get("vendor_name") or "HTP"},
        "description": {"zh_cn": p.get("description_zh") or "", "en_us": p.get("description_en") or ""},
        "change_log": {"zh_cn": p.get("change_log") or "", "en_us": p.get("change_log") or ""},
        "arch": int(p.get("arch") or 1),
        "container": "",  # 手工插件固定宿主机执行，容器需 TTY
        "hci_version": p.get("hci_version") or "",
        "acli_version": p.get("acli_version") or ">=1.0.0",
        "dependencies": {},
        "hash_algo": 1,
        "hash": hashes,
    }


def _register_item(p: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    return {
        "id": p["plugin_uuid"],
        "name": p["plugin_name"],
        "version": p.get("version") or "0.0.1",
        "status": 1,
        "source": int(p.get("source") or 3),
        "built_in": False,
        "last_updated_at": now,
    }


def _build_deploy_script(p: dict[str, Any], desc: dict[str, Any]) -> str:
    """生成在客户 HCI 上执行的原子部署脚本（含备份、校验、回滚、生效验证）。"""
    name = p["plugin_name"]
    deploy_id = desc["deploy_id"]
    tmp = f"{TMP_BASE}/{deploy_id}"
    q = shlex.quote
    lines = [
        "#!/bin/bash",
        f"# HTP acli plugin deploy {deploy_id} — generated by conversation-service",
        "set -u",
        f"BASE={q(PLUGIN_BASE)}",
        f"REG={q(REGISTER_PATH)}",
        f"COMPL={q(COMPLETION_PATH)}",
        f"TMP={q(tmp)}",
        f"NAME={q(name)}",
        f"DEPLOY_ID={q(deploy_id)}",
        f"VERSION={q(str(p.get('version') or '0.0.1'))}",
        f"SPEC_HASH={q(desc['spec_hash'])}",
        f"ENABLE_COMPLETION={q('1' if p.get('enable_completion', True) else '0')}",
        f"SMOKE_ARGS={q(str(p.get('smoke_args') or ''))}",
        "STATUS=failed",
        "ERROR=",
        "rollback() {",
        '  [ -f "$TMP/backup/plugin_register.json" ] && cp -a "$TMP/backup/plugin_register.json" "$REG"',
        '  [ -f "$TMP/backup/bash_completion.conf" ] && cp -a "$TMP/backup/bash_completion.conf" "$COMPL"',
        '  [ -d "$BASE/$NAME" ] && rm -rf "$BASE/$NAME"',
        '  if [ -d "$TMP/backup/$NAME" ]; then cp -a "$TMP/backup/$NAME" "$BASE/$NAME"; fi',
        '  if [ -d "$BASE/$NAME.old.$DEPLOY_ID" ]; then mv "$BASE/$NAME.old.$DEPLOY_ID" "$BASE/$NAME"; fi',
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
        "  exit 0",
        "}",
        "fail() {",
        '  ERROR="$1"',
        '  echo "ACP_ERROR=$1" >&2',
        "  rollback",
        '  STATUS="rolled_back"',
        "  finish",
        "}",
        'mkdir -p "$TMP/backup" "$TMP/stage/$NAME/bin" || { echo "ACP_ERROR=mkdir_failed" >&2; exit 1; }',
        'exec 9>"$BASE/plugin_register.lock" || fail "lock_open_failed"',
        'flock -w 30 9 || fail "lock_timeout"',
    ]
    for item in desc["files"]:
        rel = item["path"][len(name) + 1:]
        lines += [
            f"mkdir -p \"$(dirname \"$TMP/stage/$NAME/{rel}\")\" || fail 'stage_mkdir_failed'",
            f"printf '%b' {_printf_arg(item['content'])} > \"$TMP/stage/$NAME/{rel}\" || fail 'file_write_failed'",
            f"printf '%s  %s\\n' {q(item['sha256'])} \"$TMP/stage/$NAME/{rel}\" | sha256sum -c - >/dev/null || fail 'hash_mismatch'",
        ]
    lines += [
        f"chmod {q('0755')} \"$TMP/stage/$NAME/plugin\" || fail 'chmod_failed'",
        'cp -a "$REG" "$TMP/backup/plugin_register.json" || fail \'register_backup_failed\'',
        '[ -f "$COMPL" ] && cp -a "$COMPL" "$TMP/backup/bash_completion.conf"',
        'if [ -d "$BASE/$NAME" ]; then cp -a "$BASE/$NAME" "$TMP/backup/"; fi',
        f"printf '%b' {_printf_arg(json.dumps(desc['register_item'], ensure_ascii=False, separators=(',', ':')))} > \"$TMP/register_item.json\" || fail 'register_item_write_failed'",
        'if [ -d "$BASE/$NAME" ]; then mv "$BASE/$NAME" "$BASE/$NAME.old.$DEPLOY_ID" || fail "old_dir_move_failed"; fi',
        'mv "$TMP/stage/$NAME" "$BASE/$NAME" || fail "stage_move_failed"',
        'jq --slurpfile item "$TMP/register_item.json" \'(. // []) | del(.[] | select(.name == $item[0].name)) + [$item[0]]\' "$REG" > "$TMP/plugin_register.json" || fail "jq_register_failed"',
        'jq -e \'type == "array"\' "$TMP/plugin_register.json" >/dev/null || fail "register_json_invalid"',
        'mv "$TMP/plugin_register.json" "$REG" || fail "register_replace_failed"',
        'if [ "$ENABLE_COMPLETION" = "1" ] && [ -f "$COMPL" ]; then grep -qx "$NAME" "$COMPL" || printf "\\n%s\\n" "$NAME" >> "$COMPL"; fi',
        f"{ACLI_BIN} --formatter json plugin list > \"$TMP/plugin_list.json\" 2>&1 || fail 'plugin_list_failed'",
        'grep -q "\\\"Name\\\": \\\"$NAME\\\"" "$TMP/plugin_list.json" || fail "plugin_not_registered"',
        f"{ACLI_BIN} --timeout 60 \"$NAME\" --help > \"$TMP/help.out\" 2>&1 || fail 'plugin_help_failed'",
        'if [ -n "$SMOKE_ARGS" ]; then /sf/bin/acli --timeout 60 "$NAME" $SMOKE_ARGS > "$TMP/smoke.out" 2>&1 || fail "plugin_smoke_failed"; [ -s "$TMP/smoke.out" ] || fail "plugin_smoke_empty"; fi',
        'rm -rf "$BASE/$NAME.old.$DEPLOY_ID"',
        'STATUS="success"',
        "finish",
        "",
    ]
    return "\n".join(lines)


def _build_descriptor(p: dict[str, Any]) -> dict[str, Any]:
    """平台侧 Builder：由插件配置生成 HCI 侧四要素与可直接下发的命令。"""
    name = p["plugin_name"]
    wrapper = _build_wrapper(p)
    files = _build_files(p, wrapper)
    manifest = _build_manifest(p, files)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    files.append({
        "path": f"{name}/manifest.json",
        "mode": "0644",
        "content": manifest_text,
        "sha256": _sha256(manifest_text),
    })
    files.sort(key=lambda x: x["path"])
    register_item = _register_item(p)
    spec_payload = {
        "plugin_name": name,
        "version": p.get("version"),
        "manifest": manifest,
        "register_item": {k: v for k, v in register_item.items() if k != "last_updated_at"},
        "options": {
            "enable_completion": p.get("enable_completion", True),
            "passthrough_help": p.get("passthrough_help", False),
            "enable_pid_lock": p.get("enable_pid_lock", False),
            "smoke_args": p.get("smoke_args") or "",
        },
        "files": {i["path"]: i["sha256"] for i in files},
    }
    spec_hash = _sha256(json.dumps(spec_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    deploy_id = "htp-acli-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    desc = {
        "descriptor_version": "1.0",
        "deploy_id": deploy_id,
        "plugin_name": name,
        "version": p.get("version") or "0.0.1",
        "spec_hash": spec_hash,
        "device_host": p.get("device_host") or "",
        "node_ip": p.get("node_ip") or "",
        "files": files,
        "manifest": manifest,
        "wrapper": wrapper,
        "register_item": register_item,
        "completion_line": name if p.get("enable_completion", True) else "",
        "verify_commands": [
            f"{ACLI_BIN} --formatter json plugin list",
            f"{ACLI_BIN} --timeout 60 {name} --help",
        ],
    }
    script = _build_deploy_script(p, desc)
    script_path = f"{TMP_BASE}/{deploy_id}.sh"
    desc["deploy_script"] = script
    desc["deploy_command"] = (
        f"printf '%b' {_printf_arg(script)} > {script_path} && bash {script_path}; "
        f"rc=$?; rm -f {script_path}; exit $rc"
    )
    desc["deploy_script_path"] = script_path
    return desc


# ── 路由 ────────────────────────────────────────────────────────────────────────

@router.get("/templates", summary="参考 24 环境已有插件与 1mt 排障工具模板")
async def get_templates() -> dict[str, Any]:
    return {
        "status": "success",
        "runtime_contract": RUNTIME_CONTRACT,
        "reference_plugins": REFERENCE_PLUGINS,
        "one_mt_template": ONE_MT_TEMPLATE,
        "reference_manifest_sample": {
            "id": "f8cd6a93-faa3-4777-92b7-d4aee1bcf536",
            "name": "asan_ops",
            "version": "1.0.0",
            "source": 1,
            "arch": 1,
            "container": "vs-cp-manager",
            "hash_algo": 1,
            "note": "24 环境实测：出厂插件 manifest，字段契约与手工插件一致，仅 source/container/built_in 不同",
        },
    }


@router.get("", summary="acli 插件列表")
async def list_plugins(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    rows = (await db.execute(select(AcliPlugin).order_by(AcliPlugin.id))).scalars().all()
    return {"status": "success", "items": [r.to_dict() for r in rows], "total": len(rows)}


@router.post("", summary="新建 acli 插件配置")
async def create_plugin(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    data = _validate(await request.json())
    exists = (await db.execute(select(AcliPlugin).where(AcliPlugin.plugin_name == data["plugin_name"]))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail=f"插件 {data['plugin_name']} 已存在")
    plugin = AcliPlugin()
    _apply(plugin, data)
    db.add(plugin)
    await db.flush()
    await db.refresh(plugin)
    await db.commit()
    logger.info(event="acli_plugin_created", plugin_name=plugin.plugin_name, plugin_id=plugin.id)
    return {"status": "success", "id": plugin.id, "item": plugin.to_dict()}


@router.put("/{plugin_id}", summary="更新 acli 插件配置")
async def update_plugin(plugin_id: int, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plugin = (await db.execute(select(AcliPlugin).where(AcliPlugin.id == plugin_id))).scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="插件配置不存在")
    data = _validate(await request.json())
    if data["plugin_name"] != plugin.plugin_name:
        dup = (await db.execute(select(AcliPlugin).where(AcliPlugin.plugin_name == data["plugin_name"]))).scalar_one_or_none()
        if dup and dup.id != plugin_id:
            raise HTTPException(status_code=409, detail="插件名已被占用")
    _apply(plugin, data)
    await db.flush()
    await db.refresh(plugin)
    await db.commit()
    logger.info(event="acli_plugin_updated", plugin_id=plugin_id, plugin_name=plugin.plugin_name)
    return {"status": "success", "item": plugin.to_dict()}


@router.delete("/{plugin_id}", summary="删除 acli 插件配置（不卸载设备侧插件）")
async def delete_plugin(plugin_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plugin = (await db.execute(select(AcliPlugin).where(AcliPlugin.id == plugin_id))).scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="插件配置不存在")
    await db.delete(plugin)
    await db.commit()
    return {"status": "success", "id": plugin_id}


@router.post("/{plugin_id}/descriptor", summary="生成下发描述与可执行部署命令")
async def build_descriptor(plugin_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plugin = (await db.execute(select(AcliPlugin).where(AcliPlugin.id == plugin_id))).scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="插件配置不存在")
    desc = _build_descriptor(plugin.to_dict())
    if plugin.spec_hash != desc["spec_hash"]:
        plugin.spec_hash = desc["spec_hash"]
        await db.flush()
        await db.commit()
    logger.info(
        event="acli_plugin_descriptor_built",
        plugin_id=plugin_id, deploy_id=desc["deploy_id"], spec_hash=desc["spec_hash"],
    )
    return {"status": "success", "descriptor": desc}


@router.get("/{plugin_id}/deployments", summary="插件下发历史")
async def list_deployments(plugin_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(AcliPluginDeployment)
            .where(AcliPluginDeployment.plugin_id == plugin_id)
            .order_by(AcliPluginDeployment.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return {"status": "success", "items": [r.to_dict() for r in rows], "total": len(rows)}


@router.post("/{plugin_id}/deployments", summary="记录 terminal_bridge 下发回执")
async def record_deployment(plugin_id: int, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plugin = (await db.execute(select(AcliPlugin).where(AcliPlugin.id == plugin_id))).scalar_one_or_none()
    if not plugin:
        raise HTTPException(status_code=404, detail="插件配置不存在")
    data = await request.json()
    deploy_id = str(data.get("deploy_id") or "")
    if not deploy_id:
        raise HTTPException(status_code=400, detail="deploy_id 必填")
    existing = (
        await db.execute(select(AcliPluginDeployment).where(AcliPluginDeployment.deploy_id == deploy_id))
    ).scalar_one_or_none()
    if existing:
        return {"status": "duplicate", "id": existing.id}
    record = AcliPluginDeployment(
        deploy_id=deploy_id,
        plugin_id=plugin_id,
        plugin_name=plugin.plugin_name,
        version=str(data.get("version") or plugin.version),
        spec_hash=str(data.get("spec_hash") or plugin.spec_hash),
        device_host=str(data.get("device_host") or plugin.device_host),
        node_ip=str(data.get("node_ip") or plugin.node_ip),
        exec_id=str(data.get("exec_id") or ""),
        trace_id=str(data.get("trace_id") or ""),
        status=str(data.get("status") or "failed"),
        exit_code=data.get("exit_code"),
        stdout=str(data.get("stdout") or "")[:20000],
        stderr=str(data.get("stderr") or "")[:20000],
        verification=data.get("verification") or {},
    )
    db.add(record)
    plugin.last_deploy_id = deploy_id
    plugin.last_deploy_status = record.status
    plugin.last_deploy_at = datetime.now(UTC)
    plugin.last_deploy_detail = {
        "exec_id": record.exec_id,
        "trace_id": record.trace_id,
        "exit_code": record.exit_code,
        "verification": record.verification,
    }
    await db.flush()
    await db.refresh(record)
    await db.commit()
    logger.info(
        event="acli_plugin_deployment_recorded",
        plugin_id=plugin_id, deploy_id=deploy_id, status=record.status,
        exec_id=record.exec_id, trace_id=record.trace_id,
    )
    return {"status": "success", "id": record.id, "item": record.to_dict()}
