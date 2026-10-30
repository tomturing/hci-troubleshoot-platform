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

# RAR5 解码器：宿主机没有 root，装不了发行版包，改用 rarlab 的静态二进制。
# 实测 p7zip 23.01 对 RAR5（v50 -m3）报 Unsupported Method，unrar 7.x 能正常解；
# 没有它，客户自打的 .rar 只能"保留原件"。幂等：已就绪直接跳过。
# 下载失败只告警不中断安装——rar 只是少数工单里的附件，不该拖垮整个部署。
install_unrar() {
  local bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
  local target="$bin_dir/unrar"
  # unrar 无参数时会打印横幅但退出码非 0，在 set -o pipefail 下直接接管道
  # 会被判成失败、每次都重装，所以先捕获输出再比对。
  if [[ -x "$target" ]]; then
    local banner
    banner=$("$target" 2>&1 || true)
    if [[ "$banner" == *UNRAR* ]]; then
      echo "unrar 已就绪：$target"
      return 0
    fi
  fi
  install -d -m 755 "$bin_dir"
  local tmp pkg
  tmp=$(mktemp -d)
  # 优先取官网当前包名，取不到时回退到已验证版本，避免网页改版让安装断掉。
  pkg=$(curl -fsSL --max-time 15 https://www.rarlab.com/download.htm 2>/dev/null \
        | grep -oE 'rarlinux-x64-[0-9]+\.tar\.gz' | head -n 1) || true
  pkg=${pkg:-rarlinux-x64-723.tar.gz}
  if ! curl -fsSL --max-time 120 -o "$tmp/rar.tgz" "https://www.rarlab.com/rar/$pkg"; then
    rm -rf "$tmp"
    echo "警告：下载 unrar 失败，RAR5 归档将只能保留原件（$pkg）" >&2
    return 0
  fi
  tar xzf "$tmp/rar.tgz" -C "$tmp"
  install -m 755 "$tmp/rar/unrar" "$target"
  rm -rf "$tmp"
  echo "已安装 unrar：$target"
}

install_unrar

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
