#!/usr/bin/env bash
# 手工解压工单日志包：把一个任意层级的归档展开成一棵可直接浏览的日志目录。
#
# 判定口径与 app/extractor.py 完全一致：
#   1) 按魔数（不是扩展名）识别格式——实测多个工单的 <工单号>.zip 实际是裸 tar，
#      按扩展名调 unzip 会 exit=9；
#   2) 嵌套归档逐层展开，每个内层包解到与它同名的兄弟目录；
#   3) 解完补权限：源端目录常是 drw-r--r--，缺 x 位就到不了下一层；
#   4) 内层归档解压成功后删除以省空间（--keep-archives 可保留），失败则保留原件；
#   5) 解压失败但已有产物时保留可读部分，只告警不整单回滚；
#   6) 输入归档本身只读不删，内层归档才在解压成功后删除。
#
# 用法：
#   ./unpack-log.sh <归档文件> [输出目录]
#
# 示例：
#   TICKET_ARCHIVE_PASSWORD='***' ./unpack-log.sh Q2026102100147.zip
#   ./unpack-log.sh -p '***' ./Q2026102100147-20261112100000.zip ./logs/Q2026102100147
#   ./unpack-log.sh -f -d 8 Q2026102100147.zip          # 覆盖已有目录，嵌套上限 8 层
#
# 选项：
#   -p PASS          归档密码（缺省读环境变量 TICKET_ARCHIVE_PASSWORD，
#                    再缺省读 ~/.config/hci-ticket-sync/ticket-sync.env）
#   -o DIR           输出目录（等价于第二个位置参数）
#   -d N             嵌套层数上限（缺省 12，与 TICKET_MAX_DEPTH 一致）
#   -f               输出目录已存在且非空时先清空
#   --keep-archives  解压成功后保留内层归档文件
#   -h               帮助
#
# 退出码：0 全部成功；1 有归档解压失败（已解出内容保留）；2 参数或环境问题。

set -euo pipefail

ARCHIVE=""
OUT=""
PASS="${TICKET_ARCHIVE_PASSWORD:-}"
MAX_DEPTH=12
FORCE=0
KEEP=0
FAILED=0
DEPTH_TRUNC=0
TMP=""
TREE=""

# 嵌套归档按扩展名发现，顺序必须长后缀在前（.tar.gz 先于 .gz）。
ARCHIVE_SUFFIXES=(.tar.gz .tgz .tar.xz .txz .tar.bz2 .tbz2 .tar .zip .rar .7z)

log() { printf '%s\n' "$*" >&2; }
warn() { printf '警告: %s\n' "$*" >&2; }
die() {
  printf '错误: %s\n' "$*" >&2
  exit 2
}

# 解压类失败用 1，与参数/环境问题（2）区分开。
fail() {
  printf '错误: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：unpack-log.sh <归档文件> [输出目录]

  -p PASS          归档密码（缺省读环境变量 TICKET_ARCHIVE_PASSWORD）
  -o DIR           输出目录（等价于第二个位置参数）
  -d N             嵌套层数上限（缺省 12）
  -f               输出目录已存在且非空时先清空
  --keep-archives  解压成功后保留内层归档文件
  -h               帮助

示例：
  TICKET_ARCHIVE_PASSWORD='***' ./unpack-log.sh Q2026102100147.zip
  ./unpack-log.sh -p '***' Q2026102100147.zip ./logs/Q2026102100147
EOF
}

# shellcheck disable=SC2329  # 由 trap EXIT 调用，静态分析看不到。
cleanup() {
  if [[ -n "$TMP" && -d "$TMP" ]]; then
    rm -rf -- "$TMP" 2>/dev/null || true
  fi
}

# 未显式给密码时复用 ticket-sync 的配置：-p > 环境变量 > env 文件。
# 解析口径与 app/config.py 的 _parse_env_file 一致（去空白、去成对引号）。
load_password_from_env_file() {
  local env_file=${TICKET_SYNC_ENV:-"$HOME/.config/hci-ticket-sync/ticket-sync.env"}
  local line value
  [[ -f "$env_file" ]] || return 0
  line=$(grep -E '^[[:space:]]*TICKET_ARCHIVE_PASSWORD[[:space:]]*=' "$env_file" 2>/dev/null | tail -n 1) || true
  [[ -n "$line" ]] || return 0
  value=${line#*=}
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  value=${value%\"}
  value=${value#\"}
  value=${value%\'}
  value=${value#\'}
  PASS=$value
  [[ -n "$PASS" ]] && log "密码: 从 $env_file 读取（-p / TICKET_ARCHIVE_PASSWORD 可覆盖）"
  return 0
}

# 绝对路径化：既消掉前导 '-'，也让相对路径在 cd 之后仍然有效。
abspath() {
  local dir base
  dir=$(cd -- "$(dirname -- "$1")" && pwd -P) || return 1
  base=$(basename -- "$1")
  printf '%s/%s' "${dir%/}" "$base"
}

strip_suffix() {
  local p=$1 lower suffix
  lower=$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')
  for suffix in "${ARCHIVE_SUFFIXES[@]}"; do
    if [[ "$lower" == *"$suffix" ]]; then
      printf '%s' "${p:0:${#p}-${#suffix}}"
      return 0
    fi
  done
  printf '%s' "$p"
}

# 按魔数判格式，扩展名只作兜底。读 8 字节 + offset 257 的 tar magic。
detect_format() {
  local f=$1 hex head5 lower
  hex=$(dd if="$f" bs=1 count=8 2>/dev/null | od -An -tx1 | tr -d ' \n') || hex=""
  case "$hex" in
    504b0304* | 504b0506* | 504b0708*) echo zip; return 0 ;;
    1f8b*) echo tar.gz; return 0 ;;
    fd377a585a00*) echo tar.xz; return 0 ;;
    425a68*) echo tar.bz2; return 0 ;;
    377abcaf271c*) echo 7z; return 0 ;;
    526172211a07*) echo rar; return 0 ;;
  esac
  head5=$(dd if="$f" bs=1 skip=257 count=5 2>/dev/null | tr -d '\000') || head5=""
  if [[ "$head5" == "ustar" ]]; then
    echo tar
    return 0
  fi
  lower=$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    *.tar.gz | *.tgz) echo tar.gz ;;
    *.tar.xz | *.txz) echo tar.xz ;;
    *.tar.bz2 | *.tbz2) echo tar.bz2 ;;
    *.tar) echo tar ;;
    *.zip) echo zip ;;
    *.rar) echo rar ;;
    *.7z) echo 7z ;;
    *) echo unknown ;;
  esac
}

# 内层包解到与它同名的兄弟目录；同名路径已被文件占用时退让为 .d，
# 绝不覆盖原始日志（HCI 常把 LOG_x 与 LOG_x.zip 一起打包）。
target_for() {
  local a=$1 base cand index=2
  base=$(strip_suffix "$a")
  if [[ ! -e "$base" || -d "$base" ]]; then
    printf '%s' "$base"
    return 0
  fi
  cand="${base}.d"
  while [[ -e "$cand" && ! -d "$cand" ]]; do
    cand="${base}.d${index}"
    index=$((index + 1))
  done
  warn "解压目标与已有文件同名，改用 $(basename -- "$cand")/"
  printf '%s' "$cand"
}

extract_to() {
  local src=$1 dst=$2 fmt=$3 args=()
  mkdir -p -- "$dst" || return 1
  case "$fmt" in
    zip)
      command -v unzip >/dev/null 2>&1 || return 1
      # 有密码才传 -P：空密码会让部分 Info-ZIP 版本把普通包也当加密包处理。
      if [[ -n "$PASS" ]]; then
        unzip -P "$PASS" -q -o "$src" -d "$dst"
      else
        unzip -q -o "$src" -d "$dst"
      fi
      ;;
    tar | tar.gz | tar.xz | tar.bz2)
      command -v tar >/dev/null 2>&1 || return 1
      # -xf 由 tar 自行识别压缩（GNU tar 与 bsdtar 都支持），
      # 显式 -xzf 对裸 tar 会因缺 gzip 头直接失败。
      tar -xf "$src" -C "$dst"
      ;;
    rar)
      if command -v unrar >/dev/null 2>&1; then
        if [[ -n "$PASS" ]]; then
          unrar x -o+ -y -idq "-p${PASS}" "$src" "$dst/" && return 0
        else
          unrar x -o+ -y -idq -p- "$src" "$dst/" && return 0
        fi
      fi
      command -v 7z >/dev/null 2>&1 || return 1
      args=(-y -bb0 "-o${dst}")
      [[ -n "$PASS" ]] && args+=("-p${PASS}")
      7z x "${args[@]}" "$src"
      ;;
    7z)
      command -v 7z >/dev/null 2>&1 || return 1
      args=(-y -bb0 "-o${dst}")
      [[ -n "$PASS" ]] && args+=("-p${PASS}")
      7z x "${args[@]}" "$src"
      ;;
    *)
      return 1
      ;;
  esac
}

fix_permissions() {
  local tree=$1
  find "$tree" -type f -exec chmod u+rw {} + 2>/dev/null || true
  find "$tree" -type d -exec chmod u+x {} + 2>/dev/null || true
}

# 逐层展开：每轮扫一遍当前树，把这一轮能解的内层包解掉，直到没有新归档。
# 失败的归档会留在树里（不删原件），因此必须记住"已尝试过"的路径，
# 否则每轮都会重解同一个坏包，一路撞到层数上限。
expand_tree() {
  local tree=$1 depth=0
  local attempted=() remaining=() a seen seen_path dst fmt left
  while :; do
    remaining=()
    while IFS= read -r -d '' a; do
      seen=0
      # bash 3.2 在 set -u 下展开空数组会报 unbound variable，必须用 + 形式。
      for seen_path in ${attempted[@]+"${attempted[@]}"}; do
        if [[ "$seen_path" == "$a" ]]; then
          seen=1
          break
        fi
      done
      if ((seen == 0)); then
        remaining+=("$a")
      fi
    done < <(
      find "$tree" -type f \
        \( -iname '*.zip' -o -iname '*.tar' -o -iname '*.tar.gz' -o -iname '*.tgz' \
        -o -iname '*.tar.xz' -o -iname '*.txz' -o -iname '*.tar.bz2' -o -iname '*.tbz2' \
        -o -iname '*.rar' -o -iname '*.7z' \) -print0 2>/dev/null | sort -z
    )
    ((${#remaining[@]})) || return 0
    if ((depth >= MAX_DEPTH)); then
      DEPTH_TRUNC=${#remaining[@]}
      warn "嵌套层数已达上限 ${MAX_DEPTH}，仍有 ${DEPTH_TRUNC} 个归档未展开（原件保留）"
      return 0
    fi
    depth=$((depth + 1))
    for a in "${remaining[@]}"; do
      attempted+=("$a")
      fmt=$(detect_format "$a")
      dst=$(target_for "$a")
      log "  第 ${depth} 层  ${a#"$tree"/}  →  $(basename -- "$dst")/"
      if extract_to "$a" "$dst" "$fmt"; then
        ((KEEP == 0)) && rm -f -- "$a"
        continue
      fi
      FAILED=$((FAILED + 1))
      left=$(find "$dst" -mindepth 1 -print -quit 2>/dev/null || true)
      if [[ -n "$left" ]]; then
        warn "解压失败但已解出部分内容，归档保留：${a#"$tree"/}（格式=${fmt}）"
      else
        warn "解压失败，归档保留：${a#"$tree"/}（格式=${fmt}）"
        rmdir -- "$dst" 2>/dev/null || true
      fi
      if [[ "$fmt" == "zip" ]]; then
        warn "  若提示密码错误，请确认 -p 或 TICKET_ARCHIVE_PASSWORD"
      fi
    done
  done
}

# 解出来的是不是日志包：log_list.json / sp_* / 任意层级的 <节点目录>/sf/log/。
looks_like_logs() {
  local tree=$1
  [[ -n $(find "$tree" -maxdepth 4 \( -name 'log_list.json' -o -name 'sp_*' \) -print -quit 2>/dev/null) ]] && return 0
  [[ -n $(find "$tree" -maxdepth 3 -type d -path '*/sf/log' -print -quit 2>/dev/null) ]] && return 0
  return 1
}

main() {
  while (($#)); do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      -p)
        [[ $# -ge 2 ]] || die "-p 缺少参数"
        PASS=$2
        shift 2
        ;;
      -o)
        [[ $# -ge 2 ]] || die "-o 缺少参数"
        OUT=$2
        shift 2
        ;;
      -d)
        [[ $# -ge 2 ]] || die "-d 缺少参数"
        MAX_DEPTH=$2
        shift 2
        ;;
      -f)
        FORCE=1
        shift
        ;;
      --keep-archives)
        KEEP=1
        shift
        ;;
      --)
        shift
        break
        ;;
      -*)
        die "未知选项 $1（-h 查看用法）"
        ;;
      *)
        if [[ -z "$ARCHIVE" ]]; then
          ARCHIVE=$1
        elif [[ -z "$OUT" ]]; then
          OUT=$1
        else
          die "多余的参数 $1"
        fi
        shift
        ;;
    esac
  done
  while (($#)); do
    if [[ -z "$ARCHIVE" ]]; then
      ARCHIVE=$1
    elif [[ -z "$OUT" ]]; then
      OUT=$1
    else
      die "多余的参数 $1"
    fi
    shift
  done

  [[ -n "$ARCHIVE" ]] || {
    usage >&2
    die "缺少归档文件参数"
  }
  [[ "$MAX_DEPTH" =~ ^[0-9]+$ ]] || die "-d 需要非负整数"
  [[ -f "$ARCHIVE" ]] || die "找不到归档文件：$ARCHIVE"
  ARCHIVE=$(abspath "$ARCHIVE") || die "无法解析归档路径"
  [[ -n "$PASS" ]] || load_password_from_env_file

  command -v unzip >/dev/null 2>&1 || warn "未找到 unzip，zip 归档将无法解压"
  command -v tar >/dev/null 2>&1 || die "未找到 tar，无法继续"

  if [[ -z "$OUT" ]]; then
    OUT="$(pwd -P)/$(basename -- "$(strip_suffix "$ARCHIVE")")"
    [[ "$(basename -- "$OUT")" == "" ]] && OUT="$(pwd -P)/${ARCHIVE##*/}.logs"
  elif [[ "$OUT" != /* ]]; then
    OUT="$(pwd -P)/$OUT"
  fi

  local top_format parent home_real
  top_format=$(detect_format "$ARCHIVE")
  [[ "$top_format" != "unknown" ]] || die "无法识别归档格式（魔数与扩展名都不匹配）：$ARCHIVE"

  # 已存在的输出目录先规范化（消掉 . / .. / 尾斜杠），否则下面的 "拒绝清空"
  # 保护会被 `-o .` 这类写法绕过，rm -rf 的威力不该赌在 rm 自身的 . / .. 检查上。
  if [[ -d "$OUT" && ! -L "$OUT" ]]; then
    OUT=$(cd -- "$OUT" && pwd -P) || die "无法解析输出目录：$OUT"
  fi
  home_real=$(cd -- "$HOME" 2>/dev/null && pwd -P || printf '%s' "$HOME")

  # 输出目录保护：只允许删空重建，且拒绝明显危险的路径。
  if [[ -e "$OUT" || -L "$OUT" ]]; then
    [[ -d "$OUT" && ! -L "$OUT" ]] || die "输出路径已存在且不是普通目录：$OUT"
    if [[ -n "$(ls -A -- "$OUT" 2>/dev/null || true)" ]]; then
      ((FORCE)) || die "输出目录已存在且非空：${OUT}（加 -f 覆盖）"
      case "$OUT" in
        / | /tmp | /private/tmp | /var | /usr | /etc | /opt | /home | /Users | /private | "$home_real" | "$(pwd -P)")
          die "拒绝清空 $OUT"
          ;;
      esac
      if [[ "$(pwd -P)" == "$OUT"/* ]]; then
        die "拒绝清空当前目录的上级：$OUT"
      fi
      log "清空已有目录 $OUT"
      rm -rf -- "$OUT" || die "无法清空 $OUT"
    fi
  fi

  parent=$(dirname -- "$OUT")
  mkdir -p -- "$parent" || die "无法创建输出目录的父目录：$parent"
  TMP=$(mktemp -d "$parent/.unpack-log.XXXXXX") || die "无法创建临时目录"
  trap cleanup EXIT
  TREE="$TMP/tree"
  mkdir -p -- "$TREE"

  log "归档: $ARCHIVE"
  log "格式: ${top_format}（按魔数判定）"
  log "密码: $([[ -n "$PASS" ]] && echo '已提供' || echo '未提供，仅无密码归档可解')"
  log "输出: $OUT"

  log "展开顶层…"
  if ! extract_to "$ARCHIVE" "$TREE" "$top_format"; then
    if [[ "$top_format" == "zip" && -z "$PASS" ]]; then
      fail "顶层归档解压失败：$(basename -- "$ARCHIVE")（加密包需 -p 或 TICKET_ARCHIVE_PASSWORD）"
    fi
    fail "顶层归档解压失败：$(basename -- "$ARCHIVE")"
  fi

  log "展开嵌套归档…"
  expand_tree "$TREE"
  fix_permissions "$TREE"

  local files_bytes size
  files_bytes=$(find "$TREE" -type f 2>/dev/null | wc -l | tr -d ' ')
  size=$(du -sh "$TREE" 2>/dev/null | awk '{print $1}')

  if ! looks_like_logs "$TREE"; then
    warn "未发现 log_list.json / sp_* / */sf/log，可能只是转储包或采集不完整"
  fi

  # 发布：输出目录此刻要么不存在，要么是空目录，rename 才是原子的。
  [[ -d "$OUT" ]] && rmdir -- "$OUT" 2>/dev/null || true
  mv -- "$TREE" "$OUT" || die "无法把解压结果移动到 $OUT"
  rmdir -- "$TMP" 2>/dev/null || true
  TMP=""

  printf '\n完成：%s\n' "$OUT"
  printf '文件数：%s  体积：%s\n' "$files_bytes" "${size:-?}"
  if ((FAILED > 0)); then
    printf '有 %s 个内层归档解压失败（已解出内容保留、原件未删），见上方警告\n' "$FAILED"
    exit 1
  fi
  if ((DEPTH_TRUNC > 0)); then
    printf '有 %s 个归档超出嵌套上限，未展开（原件保留）\n' "$DEPTH_TRUNC"
  fi
  exit 0
}

main "$@"
