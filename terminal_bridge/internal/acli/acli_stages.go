package acli

// acli_stages.go 把安装链路里"判定"与"执行"分开的可测部分显式化。
//
// 为什么逐段抽而不是一次性重排 HandleAcliSync：那条主流程与 *sshx.SSHSession
// 深度耦合，一次性重排后没有任何东西能证明失败分支行为不变。抽一个纯函数、
// 补一组分支测试、再抽下一个——每步都能编译、能单测、能回滚。

import (
	"fmt"
	"strings"
	"time"

	"terminal_bridge/internal/shared/logcontract"
)

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

// acliRemoteProbe 是「节点上那份安装包」的探测结果。
type acliRemoteProbe struct {
	Size    int64
	Algo    string
	Digest  string
	Missing bool
	Err     error
}

// acliReuseDecision 是「能否复用节点上已有安装包」的结论。
type acliReuseDecision struct {
	Ready  bool
	Reason string // probe_failed | missing | verified | rejected
	Detail string // rejected 时的具体不符点
}

// decideAcliReuse 决定重跑时能否复用节点上已有的安装包。
//
// 为什么必须显式给出 reason：上一轮正是因为「盘上那份包不对」才失败的，
// 无条件复用等于把同一个错误再执行一遍。三种降级原因在排障上含义完全不同：
// 探测失败可能是通道问题（可再试）、包不存在需要重传、与基线不符说明盘上那份
// 就是坏包（必须重传且不能再复用）。
func decideAcliReuse(retryStage string, probe acliRemoteProbe, baseline *acliPackage) acliReuseDecision {
	if retryStage == "all" {
		// 完整流程不探测节点，直接把"需要复用"判为否，也不产生留痕原因。
		return acliReuseDecision{}
	}
	switch {
	case probe.Err != nil:
		return acliReuseDecision{Reason: "probe_failed"}
	case probe.Missing:
		return acliReuseDecision{Reason: "missing"}
	}
	if err := verifyReusedRemotePackage(baseline, probe.Size, probe.Algo, probe.Digest); err != nil {
		return acliReuseDecision{Reason: "rejected", Detail: errString(err)}
	}
	return acliReuseDecision{Ready: true, Reason: "verified"}
}

// acliPackageAction 决定本轮重试从哪里拿包。
type acliPackageAction int

const (
	// packageRefetch 重新从官方源下载：缓存不可信，或本轮本来就不该用缓存。
	packageRefetch acliPackageAction = iota
	// packageFromCache 复用本进程已校验过的完整字节（仅「只重传」且缓存有 Data）。
	packageFromCache
)

// planAcliPackage 决定本轮用什么包。
//
// 只有「只重传」且缓存里确实有完整字节时才复用缓存：
//   - retryStage=install 时用户要的是「重装」而非「重传」，内存里的字节没有意义，
//     真正的复用发生在阶段 8（节点上那份包与基线比对）；
//   - 缓存里 Data 为空的条目是「基线包」（来自账本恢复），只能当校验基线，
//     拿去当上传字节会传上去 0 字节——对外表现是"上传成功但节点上是空文件"。
func planAcliPackage(retryStage string, cached *acliPackage) acliPackageAction {
	if retryStage == "upload" && cached != nil && cached.Data != nil {
		return packageFromCache
	}
	return packageRefetch
}

// acliRetryBackoff 是第 attempt 次失败后的等待时长（线性，attempt × 2s）。
//
// 为什么不用指数退避：只有 3 次尝试，指数退避省不下几秒，却会把
// 「用户点了按钮在等」的观感变成假死；线性 2s/4s 足够让瞬时抖动过去。
func acliRetryBackoff(attempt int) time.Duration {
	return time.Duration(attempt) * 2 * time.Second
}

// acliShouldRetry 报告第 attempt 次失败后是否还有下一轮。
//
// 单独成函数是为了让「上限」只有一处：硬编码在循环条件与退避判断两处时，
// 改上限会漏掉一处，表现为"最后一次失败后还会再等一轮再放弃"。
func acliShouldRetry(attempt int) bool {
	return attempt < acliMaxAttempts
}

// evalAcliVerifyOutcome 判定安装后的版本验证是否成立。
//
// 两个条件缺一不可：退出码 0 只证明 `acli --version` 跑通，ver 非空才证明
// 安装后的入口真的可执行。只判退出码会把「命令能跑但解析不出产品版本」
// 当成成功——回执写着"安装成功"，用户点开终端却是空的。
//
// 判定失败时也要返回已解析出的 ver/build：回执需要展示"到底探测到了什么"，
// 只回一句"验证失败"会迫使排障者登录节点重跑一次。
func evalAcliVerifyOutcome(out string, exitCode int) (ver, build string, ok bool) {
	ver, build = ParseAcliVersion(out)
	return ver, build, exitCode == 0 && ver != ""
}

// acliInstallVerdict 是安装命令结束后的结论：成功，或失败时的错误码与可读说明。
type acliInstallVerdict struct {
	OK     bool
	Code   string
	Detail string
}

// evalAcliInstallOutcome 把安装结果收敛成回执需要的结论。
//
// 三种失败必须分开表达，否则现场只能看到「安装失败」：
//   - 超时：预算耗尽，通常意味着集群分发慢，与包无关；
//   - 有错误但无退出码：SSH 通道在安装中途断开（远端 shell 已退出）；
//   - 退出码非零：安装器自己判失败，需要看 stderr。
func evalAcliInstallOutcome(outcome acliInstallOutcome, timedOut bool) acliInstallVerdict {
	if outcome.Err == nil && outcome.ExitCode == 0 {
		return acliInstallVerdict{OK: true}
	}
	if timedOut {
		return acliInstallVerdict{
			Code:   logcontract.ErrInstallExec,
			Detail: fmt.Sprintf("acli 安装命令超时（预算 %s）", acliInstallTimeout),
		}
	}
	if outcome.Err != nil && outcome.ExitCode == 0 {
		return acliInstallVerdict{
			Code:   logcontract.ErrInstallExec,
			Detail: "acli 安装命令异常中断（SSH 通道中断或远端 shell 退出）",
		}
	}
	return acliInstallVerdict{
		Code:   logcontract.ErrInstallExec,
		Detail: fmt.Sprintf("acli 安装命令退出码 %d", outcome.ExitCode),
	}
}
