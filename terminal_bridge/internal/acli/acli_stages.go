package acli

// acli_stages.go 把安装链路里"判定"与"执行"分开的可测部分显式化。
//
// 为什么逐段抽而不是一次性重排 HandleAcliSync：那条主流程与 *sshx.SSHSession
// 深度耦合，一次性重排后没有任何东西能证明失败分支行为不变。抽一个纯函数、
// 补一组分支测试、再抽下一个——每步都能编译、能单测、能回滚。

import (
	"fmt"
	"strings"

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
