#!/usr/bin/env bash
# 以当前登录用户安装 HCI 工单日志同步服务（不需要 root）。
#
# 产物：
#   ~/.local/share/hci-ticket-sync/        代码与 .venv
#   ~/.config/hci-ticket-sync/ticket-sync.env   配置（600，缺失时从模板生成并停止安装）
#   ~/.config/systemd/user/hci-ticket-sync.service
#
# 幂等：可反复执行；只覆盖代码与 unit，不碰已有的 env 文件与 .venv 里的包缓存。
#
# 前置：/sf/tickets 已由 node 创建；`uv` 可用；`loginctl enable-linger $USER` 成功
#       （否则重启后用户单元不会自启，脚本会在末尾给出告警）。
set -euo pipefail

src=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/hci-ticket-sync"
conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/hci-ticket-sync"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
env_file="$conf_dir/ticket-sync.env"

install -d -m 755 "$data_dir" "$unit_dir"
install -d -m 700 "$conf_dir"

# 代码只同步 app 包与依赖声明；.venv 由 uv 自己维护，删掉重装代价太大。
# 先确认变量非空再 rm -rf：这里拼的是 $HOME，写错一个字符就是灾难。
[[ -n "$data_dir" && "$data_dir" == */hci-ticket-sync ]] || { echo "数据目录异常：$data_dir" >&2; exit 1; }
rm -rf "$data_dir/app"
cp -a "$src/app" "$data_dir/app"
install -m 644 "$src/pyproject.toml" "$src/uv.lock" "$data_dir/"

# --frozen 严格按 uv.lock 装，--no-dev 不带测试依赖，避免把 pytest 装进生产环境。
(cd "$data_dir" && uv sync --frozen --no-dev --quiet)

if [[ ! -f "$env_file" ]]; then
  install -m 600 "$src/deploy/ticket-sync.env.example" "$env_file"
  echo "首次安装：已生成 $env_file，请填入 TICKET_ARCHIVE_PASSWORD 等值后重新运行本脚本" >&2
  exit 1
fi

# 归档密码允许为空（dry-run 不需要），但真正解压会失败，这里提前拦一道。
if ! grep -q '^TICKET_ARCHIVE_PASSWORD=.\+' "$env_file"; then
  echo "配置缺少 TICKET_ARCHIVE_PASSWORD：$env_file" >&2
  exit 1
fi

install -m 644 "$src/deploy/hci-ticket-sync.user.service" "$unit_dir/hci-ticket-sync.service"

systemctl --user daemon-reload
# enable 只建开机链接；restart 保证重跑脚本时新代码真的生效（--now 对已在跑的服务不重启）。
systemctl --user enable hci-ticket-sync.service
systemctl --user restart hci-ticket-sync.service

# 关键前提：没有 linger，用户管理器只在登录后存在，重启后服务不会自启。
if ! loginctl enable-linger "$USER" 2>/dev/null; then
  echo "警告：无法开启 linger，重启后服务不会自启；请让有 root 的同事执行：" >&2
  echo "  loginctl enable-linger $USER" >&2
elif [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo "警告：linger 未生效，重启后服务不会自启" >&2
fi

echo "已安装。查看状态：systemctl --user status hci-ticket-sync"
echo "查看日志：journalctl --user -u hci-ticket-sync -f"
