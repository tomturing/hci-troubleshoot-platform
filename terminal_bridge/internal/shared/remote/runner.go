// Package remote 是节点面横向原语：把字节写过去、探测文件在不在、执行远端命令、
// 拉取大文件。它不引入任何"期望值"语义——期望态属 reconcile 包。
//
// 分层硬约束（节点同步链路统一收口设计 §2.1 / §七）：
//   - remote 禁止 import main 包与 reconcile 包；
//   - main 包的 SSHSession 通过下面的 Runner 接口缝适配进来，
//     因此 WriteFile(ctx, s SSHSession, ...) 这种签名在 Go 里根本不会出现。
package remote

import (
	"bytes"
	"context"
	"io"
	"sync"
	"time"
)

// Runner 是 remote/ 认识的最小执行接口缝（零逻辑，只有端口定义）。
//
// 两个方法与 main 包 SSHSession 的同名方法同签名，适配是零成本包装：
//   - RunQuickCommand：远端命令执行，返回 (stdout, exitCode, err)。
//     历史契约：stdout 为空时用 stderr 兜底当输出（调用方只看一个字符串）。
//   - WriteStdin：向远端命令的 stdin 流式写入，是 `cat > tmp` 的字节通道。
type Runner interface {
	RunQuickCommand(ctx context.Context, command string) (stdout string, exitCode int, err error)
	WriteStdin(ctx context.Context, command string, r io.Reader) (stdout string, exitCode int, err error)
}

// StreamsRunner 是可选扩展：能把 stdout 与 stderr 分轨返回的执行器。
//
// 为什么做成可选而不是塞进 Runner：分轨不是所有执行器都有的能力，而
// 「节点上没有这个命令」(exit 127) 与「有命令但没有这个文件」两种失败
// 的结论完全相反，合并成一条字符串后不可分辨（Q2026102900165 现场
// "包在盘上却被判不存在"的根因）。需要分轨的原语用类型断言取它，
// 取不到时显式降级并在结果里标注，而不是静默当成同一种失败。
type StreamsRunner interface {
	Runner
	RunQuickCommandStreams(ctx context.Context, command string) (stdout, stderr string, exitCode int, err error)
}

// WaitFunc 等待流式命令结束并返回其错误（含远端退出码语义）。
type WaitFunc func() error

// StreamRunner 是可选扩展：以流的方式读取远端命令的 stdout。
// 为什么 Runner 的两个方法表达不了拉取：20 GiB 的压缩包若先收进 string
// 再落盘，等于把整包放进桥进程内存；而边读边写边摘要要求"读到一段就处理
// 一段"。因此拉取原语需要 io.ReadCloser 而不是 string。
//
// 返回的 ReadCloser.Close 必须能终止远端命令（不只是关闭本地管道），
// 否则 ctx 取消后节点上的 cat/tail 仍在跑，白占带宽与文件句柄。
type StreamRunner interface {
	Runner
	ReadStream(ctx context.Context, command string) (io.ReadCloser, WaitFunc, error)
}

// QuickRunner 是「只要一次性执行结果」的最小端口。
//
// 为什么不是 Runner：Runner 还要求 WriteStdin（`cat > tmp` 的字节通道），
// 而 sshx.SSHSession 的这个能力在 sshRunner 适配器上，不在会话类型上。
// 集群扇出、节点清单探测、插件落盘校验都只需要命令与退出码，
// 用最小端口就能让 *sshx.SSHSession 零包装地适配进来。
type QuickRunner interface {
	RunQuickCommand(ctx context.Context, command string) (stdout string, exitCode int, err error)
}

// QuickStreamsRunner 是分轨（stdout/stderr 分开）的一次性执行端口。
type QuickStreamsRunner interface {
	QuickRunner
	RunQuickCommandStreams(ctx context.Context, command string) (stdout, stderr string, exitCode int, err error)
}

// PtyRunner 是可选扩展：在 PTY 里执行命令。
//
// 为什么需要 PTY：`acli --formatter json ...` 这类命令在非交互会话里可能被截断或
// 拒绝输出（受限 shell），PTY 能拿到完整输出；`--version` 与 node list 都靠它。
// 端口与 sshx.SSHSession.RunCommandPty 同签名，适配零成本；把它定义在 remote
// 是为了让 shared/cluster 不必 import sshx 具体类型（shared 层硬约束）。
type PtyRunner interface {
	RunCommandPty(ctx context.Context, command string, timeout time.Duration) (stdout string, exitCode int, err error)
}

// ProgressFunc 报告已写入字节数（sent/total）。nil 表示不上报。
type ProgressFunc func(sent, total int64)

// ProgressReader 包装 io.Reader 并按步长触发进度回调。
//
// 回调在锁外执行：回调里若要发 WS 消息，持锁发送会把上传阻塞在网络往返上。
type ProgressReader struct {
	reader io.Reader
	total  int64
	read   int64
	step   int64
	next   int64
	mu     sync.Mutex
	onStep ProgressFunc
}

// NewProgressReader 构造进度读取器；step<=0 时回退默认 2MiB。
func NewProgressReader(data []byte, step int64, onStep ProgressFunc) *ProgressReader {
	if step <= 0 {
		step = DefaultProgressStep
	}
	return &ProgressReader{
		reader: bytes.NewReader(data),
		total:  int64(len(data)),
		step:   step,
		next:   step,
		onStep: onStep,
	}
}

func (p *ProgressReader) Read(b []byte) (int, error) {
	n, err := p.reader.Read(b)
	if n > 0 {
		p.mu.Lock()
		p.read += int64(n)
		sent := p.read
		fire := p.onStep != nil && sent >= p.next
		if fire {
			for p.next <= sent {
				p.next += p.step
			}
		}
		p.mu.Unlock()
		if fire {
			p.onStep(sent, p.total)
		}
	}
	return n, err
}
