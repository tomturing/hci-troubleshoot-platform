#!/bin/bash
# 一键切换 5888 的 P1 直传鉴权模式（两阶段，中文注释）
#   on  = 强制凭据：acli 修好 headers/auth 并分发后启用
#   off = 临时放宽：当前阶段，允许无认证头的 acli 直传
set -euo pipefail
MODE="${1:-}"
case "$MODE" in
  on)  VAL=1; DESC='强制凭据（P1 票据 / 全局 Bearer）' ;;
  off) VAL=0; DESC='临时放宽（允许匿名直传）' ;;
  *) echo "用法: sudo $0 on|off" >&2; exit 1 ;;
esac
CONF=/etc/systemd/system/acli-5888.service.d/p1-auth.conf
install -d -m 0755 "$(dirname "$CONF")"
printf '[Service]\nEnvironment=ACLI_P1_REQUIRE_AUTH=%s\n' "$VAL" > "$CONF"
systemctl daemon-reload
systemctl restart acli-5888
sleep 2
systemctl is-active acli-5888
echo "P1 鉴权模式已切换: $DESC (ACLI_P1_REQUIRE_AUTH=$VAL)"
