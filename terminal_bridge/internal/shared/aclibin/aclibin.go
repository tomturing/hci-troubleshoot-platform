// Package aclibin 是「节点上实际在跑哪一份 acli」的唯一事实源。
//
// 为什么独立成包：这套 PATH 前缀 + 绝对路径解析 + 输出协议同时被两个业务域需要——
// logcollect 用来下发 create/progress/cancel，acli 域用来显示当前版本。改造前它住在
// logcollect 里，acli 只能反向依赖采集域；若改为各写一份，就会重新引入历史缺陷：
// 探测走 /sf/bin/acli，执行却裸写 `acli`，sshd 非交互会话 PATH 不含 /sf/bin 时
// 报 command not found，表现为「探测通过、一采集就失败」（2026-10-27 现场）。
//
// 归属判据：它既不是通用工具（删掉会产生正确性风险），也不属于任一业务域，
// 而是两个域共用的**事实层**，因此放在 shared 叶子包，纯字符串、零 I/O。
package aclibin

import "strings"

// PathEnv 是 acli 的固定调用前缀：容器 PATH 不含 /sf/bin，缺了它裸命令必然失败。
const PathEnv = `export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin";`

// Candidates 是 acli 二进制的解析顺序（HCI 官方入口在前）。
//
// PATH 只能"追加"，而 sshd 非交互会话的默认 PATH
// （/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin）排在我们的追加项之前。
// 一旦节点上存在两份 acli（旧版落在 /usr/local/bin 等目录），`acli --version` 命中的是
// 旧版，于是面板显示的版本与运维在登录 shell 里手敲的结果不一致，且无法自证。
// 因此所有 acli 调用都按绝对路径解析，并把真正命中的路径一起回传。
var Candidates = []string{"/sf/bin/acli", "/sf/sbin/acli"}

// BinPrefix 是解析结果行前缀；AbsentToken 表示节点上没有 acli。
const (
	BinPrefix   = "__ACLI_BIN__="
	AbsentToken = "__ACLI_ABSENT__"
)

// ResolveScript 返回一段 POSIX sh：把命中的 acli 绝对路径写入 $__acli，
// 未命中时置 __acli_resolve_absent=1。
//
// 顺序固定为：绝对路径候选（/sf/bin → /sf/sbin）→ `command -v acli` 兜底。
func ResolveScript() string {
	candidates := strings.Join(Candidates, " ")
	return `__acli_resolve_absent=1; __acli=""; for p in ` + candidates +
		`; do if [ -x "$p" ]; then __acli="$p"; __acli_resolve_absent=0; break; fi; done;` +
		` if [ -z "$__acli" ]; then __acli=$(command -v acli 2>/dev/null);` +
		` if [ -n "$__acli" ]; then __acli_resolve_absent=0; fi; fi;`
}

// Invoke 把「acli 之后的参数串」包成可直接下发的自解析命令。
//
// 契约：args 不含 acli 本体（例如 `log_collect progress`）；返回串以 PATH 环境开头、
// 以 `"$__acli" <args>` 结尾，解析失败时输出 __ACLI_ABSENT__ 并以 127 退出，
// 使调用方拿到与"命令不存在"一致的失败语义（而不是误判成插件业务失败）。
func Invoke(args string) string {
	return PathEnv + " " + ResolveScript() +
		` if [ "$__acli_resolve_absent" = "1" ]; then echo ` + AbsentToken + ` >&2; exit 127; fi; "$__acli" ` + args
}

// VersionCommand 生成既解析二进制路径、又输出原始版本的两段式命令。
//
// 输出契约：
//
//	__ACLI_BIN__=/sf/bin/acli
//	1.0.1
//	build 2026-09-15 21:00:04
//
// 候选都不存在时输出 __ACLI_ABSENT__，保证"未安装"的判定口径与旧实现一致。
// 显示值与实际执行必须是同一份二进制，否则会出现"面板显示 1.0.1、采集却命中旧版"的错位。
func VersionCommand() string {
	return PathEnv + " " + ResolveScript() +
		` if [ "$__acli_resolve_absent" = "1" ]; then echo ` + AbsentToken +
		`; else printf '` + BinPrefix + `%s\n' "$__acli"; "$__acli" --version 2>&1; fi`
}

// ParseBinary 从探测输出中取出实际命中的 acli 路径；取不到返回空串。
func ParseBinary(output string) string {
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, BinPrefix) {
			return strings.TrimSpace(strings.TrimPrefix(line, BinPrefix))
		}
	}
	return ""
}
