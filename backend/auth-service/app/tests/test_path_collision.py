"""认证路径不得与观测 chart（Langfuse）的 Ingress 路径冲突。

背景：auth-service 原使用 \`/api/auth/*\`，而观测 chart 的 Langfuse Ingress
（\`deploy/helm/hci-platform-obs/templates/ingress-langfuse.yaml\`）在**同一入口
（web=4888）、同一 Host（acli.sangfor.com.cn）**下占用了 \`/api/auth\`（NextAuth 登录）。
Traefik 按「Host 更具体 + 路径最长前缀」优先匹配 → 管理台登录 POST 被 Langfuse 截走，
返回 400 \`This action with HTTP POST is not supported by NextAuth\`。

⚠️ 该冲突只在"带真实 Host 头"时出现：用 IP 直连（Host=IP）验证会完全绕过 Langfuse 的
Host 规则，表现为 200——是典型的验证盲区。因此用静态契约钉死，不依赖人工联调发现。
"""

from __future__ import annotations

import re
from pathlib import Path

# app/tests → app → auth-service → backend → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]

AUTH_ROUTES = REPO_ROOT / "backend" / "auth-service" / "app" / "routes" / "auth.py"
GATEWAY_ROUTES = REPO_ROOT / "backend" / "api-gateway" / "app" / "routes" / "auth.py"
LANGFUSE_INGRESS = (
    REPO_ROOT / "deploy" / "helm" / "hci-platform-obs" / "templates" / "ingress-langfuse.yaml"
)

_ROUTE_RE = re.compile(r'@router\.(?:post|get|put|delete|patch)\("([^"]+)"')
_PROXY_RE = re.compile(r'"(/api/[^"]*)\{path:path\}"')
_INGRESS_PATH_RE = re.compile(r"^\s*- path:\s*(\S+)", re.M)


def auth_paths() -> set[str]:
    """认证相关路径：auth-service 路由 + 网关代理前缀。"""
    found: set[str] = set()
    for file in (AUTH_ROUTES, GATEWAY_ROUTES):
        content = file.read_text(encoding="utf-8")
        found.update(_ROUTE_RE.findall(content))
        found.update(_PROXY_RE.findall(content))
    return {path.rstrip("/") for path in found}


def langfuse_paths() -> set[str]:
    """Langfuse Ingress 声明的路径（含 NextAuth 的 /api/auth）。"""
    return set(_INGRESS_PATH_RE.findall(LANGFUSE_INGRESS.read_text(encoding="utf-8")))


def test_auth_paths_do_not_collide_with_langfuse() -> None:
    """认证路径与被 Langfuse 占用的路径不得互为前缀。"""
    collisions = [
        (auth, lang)
        for auth in auth_paths()
        for lang in langfuse_paths()
        if auth == lang or auth.startswith(lang + "/") or lang.startswith(auth + "/")
    ]
    assert not collisions, (
        "认证路径与 Langfuse Ingress 路径冲突，同一入口下 Traefik 会按最长前缀把请求截走："
        f"{collisions}"
    )


def test_contract_finds_both_sides() -> None:
    """防止正则/路径失效导致契约变成空集合而通过。"""
    assert auth_paths(), "未解析到认证路径，正则或文件位置已变化"
    assert langfuse_paths(), "未解析到 Langfuse Ingress 路径，正则或文件位置已变化"
