package remote

import (
	"context"
	"strconv"
	"strings"
	"time"
)

// S0：exec 语义差异表（收口设计 §六 S0）。remote/exec 只提供最小语义，
// 历史差异由包装层显式提供，**不做"一个函数兼容所有历史行为"**：
//
//	差异点     | runQuickCommand | log_collect 内调用 | delivery 内调用
//	-----------|-----------------|--------------------|----------------
//	stderr     | stdout 空时兜底  | 合并               | 分轨
//	退出码     | 返回 int        | 返回 int           | 部分仅判 error
//	超时归属   | 调用方 ctx      | 调用方 ctx         | 内置 pace 退避
//	输出上限   | 无              | 有截断             | 无
//
// 对应包装层：Run（最小）/ RunCombined（合并）/ RunTruncated（截断）/
// RunSplit（分轨）。pace 退避属调用方编排，不进原语。

// ExecResult 是一次远端命令执行的完整事实。
type ExecResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
	Duration time.Duration
	// SplitDegraded 为真表示执行器不支持分轨，Stderr 是兜底合并后的产物。
	// 静默降级等于把"分不清两种失败"伪装成"分清了"。
	SplitDegraded bool
	// Truncated 为真表示输出被截断（原始字节数见 FullBytes）。
	Truncated bool
	FullBytes int
}

// Run 执行远端命令，返回最小语义：stdout / exitCode / err。
//
// stdout 为空时用 stderr 兜底是历史契约（调用方只看一个字符串），
// 由 Runner 的实现保证；本函数不再二次加工，避免两层各兜一次底。
func Run(ctx context.Context, r Runner, command string) ExecResult {
	start := time.Now()
	stdout, code, err := r.RunQuickCommand(ctx, command)
	return ExecResult{Stdout: stdout, ExitCode: code, Err: err, Duration: time.Since(start)}
}

// RunSplit 分轨执行：stdout 与 stderr 各自返回。
//
// 用于必须区分「命令不存在」(exit 126/127) 与「文件不存在」的探测场景。
// 执行器不支持分轨时显式标注 SplitDegraded，而不是假装分清了。
func RunSplit(ctx context.Context, r Runner, command string) ExecResult {
	start := time.Now()
	if sr, ok := r.(StreamsRunner); ok {
		stdout, stderr, code, err := sr.RunQuickCommandStreams(ctx, command)
		return ExecResult{Stdout: stdout, Stderr: stderr, ExitCode: code, Err: err, Duration: time.Since(start)}
	}
	res := Run(ctx, r, command)
	res.Duration = time.Since(start)
	res.SplitDegraded = true
	return res
}

// RunCombined 合并 stdout 与 stderr（log_collect 的历史语义）。
//
// 合并只在"给人看/给日志看"时成立；判定成败仍看 ExitCode。
func RunCombined(ctx context.Context, r Runner, command string) ExecResult {
	res := RunSplit(ctx, r, command)
	combined := res.Stdout
	if strings.TrimSpace(res.Stderr) != "" {
		if combined != "" {
			combined += "\n"
		}
		combined += res.Stderr
	}
	res.Stdout = combined
	res.FullBytes = len(combined)
	return res
}

// RunTruncated 在合并语义之上按上限截断输出（log_collect 的历史语义）。
//
// 截断形态与既有 truncateString 一致：保留前 limit 字节 + "..."。
// limit<=0 表示不截断。FullBytes 保留原始长度，供"日志被截了多少"可查。
func RunTruncated(ctx context.Context, r Runner, command string, limit int) ExecResult {
	res := RunCombined(ctx, r, command)
	res.FullBytes = len(res.Stdout)
	if limit > 0 && len(res.Stdout) > limit {
		res.Stdout = res.Stdout[:limit] + "..."
		res.Truncated = true
	}
	return res
}

// ChunkOutcome 是 RunChunked 的逐片结果。
type ChunkOutcome struct {
	// Index 是失败/停止处的分片下标；全部成功时为 len(commands)。
	Index int
	// Results 是已执行分片的逐条事实（含各自 exit_code）。
	Results []ExecResult
	// Err 非空表示执行中断（通道错误或某片非零退出）。
	Err error
}

// RunChunked 逐条执行分片命令并逐条查 exit_code。
//
// 与文件传输分片（transfer.Pull）的失败语义不可混在一个函数里：
// 这里是"命令退出码"，那里是"HTTP 分片回执"。任一片非零即停，
// 已执行片的结果全部保留——否则排障只能看到"失败了"，看不到"失败前写了什么"。
func RunChunked(ctx context.Context, r Runner, commands []string) ChunkOutcome {
	out := ChunkOutcome{Results: make([]ExecResult, 0, len(commands))}
	for index, command := range commands {
		if err := ctx.Err(); err != nil {
			out.Index, out.Err = index, err
			return out
		}
		res := Run(ctx, r, command)
		out.Results = append(out.Results, res)
		if res.Err != nil || res.ExitCode != 0 {
			out.Index = index
			out.Err = res.Err
			if out.Err == nil {
				out.Err = &ExitCodeError{Command: command, ExitCode: res.ExitCode, Output: res.Stdout}
			}
			return out
		}
	}
	out.Index = len(commands)
	return out
}

// ExitCodeError 表达"命令跑完了但退出码非零"，与通道错误区分开。
type ExitCodeError struct {
	Command  string
	ExitCode int
	Output   string
}

func (e *ExitCodeError) Error() string {
	return "远端命令退出码 " + strconv.Itoa(e.ExitCode) + "：" + truncateHead(e.Output, 200)
}

func truncateHead(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	return s[:limit] + "..."
}
