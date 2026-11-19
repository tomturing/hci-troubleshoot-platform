package remote

import "strings"

// ShellQuote 是全仓唯一的 shell 参数转义函数（收口设计 §4.4）。
//
// 用单引号包裹并按 POSIX 规则转义内部单引号：单引号内 $、反引号、双引号、
// 星号都不展开，因此任何用户输入（工单号、路径、配置原文、插件参数）
// 经它包裹后都只能是字面量，不可能变成第二条命令。
//
// 收口前的两处实现：log_collect.go 的 shellQuote（原样迁移）与 main.go 的
// terminal.VmConsoleShellQuote（对"安全字符"省略引号）。省略引号与加引号在 shell 里
// 行为等价，但两套实现意味着两个注入面各自演化，故统一为总是加引号；
// VM console 的调用方按此适配（其单测断言随之改为带引号形态）。
func ShellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}
