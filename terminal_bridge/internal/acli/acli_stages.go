package acli

// acli_stages.go 把安装链路里"判定"与"执行"分开的可测部分显式化。
//
// 为什么逐段抽而不是一次性重排 HandleAcliSync：那条主流程与 *sshx.SSHSession
// 深度耦合，一次性重排后没有任何东西能证明失败分支行为不变。抽一个纯函数、
// 补一组分支测试、再抽下一个——每步都能编译、能单测、能回滚。

import "strings"

// normalizeAcliRetryStage 归一化前端传入的重试起点。
//
// 合法值只有 all / upload / install；非法值一律回 all：宁可多做一次完整校验，
// 也不要因为一个笔误走了未校验的快捷路径（upload 会跳过下载，install 会跳过整个传输）。
//
// 单独成函数是为了让"哪些值算合法"只有一处定义：这两个值直接决定后面能否跳过
// 下载或跳过传输，散在分支里改一处漏一处就会造出未校验的快路径。
func normalizeAcliRetryStage(raw string) string {
	stage := strings.ToLower(strings.TrimSpace(raw))
	if stage == "upload" || stage == "install" {
		return stage
	}
	return "all"
}

// parseTargetArch 把 `uname -m` 的输出收敛成下载用的目标架构。
//
// supported=false 表示暂不支持：调用方必须 fail-closed 直接拒绝，
// 不能"猜一个架构"去下载——装错架构的包在节点上会以 exec format error 失败，
// 而那个错误码与"包损坏"难以区分，会把排障引到错误方向。
//
// 同时接受 x86_64/amd64 与 aarch64/arm64 四种写法：不同发行版与容器里
// uname 会给出不同拼写，漏掉任一种都会把"能装的机器"判成暂不支持。
func parseTargetArch(unameOut string) (arch string, supported bool) {
	a := strings.ToLower(strings.TrimSpace(unameOut))
	switch {
	case strings.Contains(a, "aarch64"), strings.Contains(a, "arm64"):
		return "aarch64", true
	case strings.Contains(a, "x86_64"), strings.Contains(a, "amd64"):
		return "x86_64", true
	}
	return a, false
}
