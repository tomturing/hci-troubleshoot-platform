package sshx

import (
	"bytes"
	"context"
	"errors"
	"io"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"

	"terminal_bridge/internal/shared/remote"
)

// ssh_runner.go —— SSHSession 到 remote.Runner 的适配缝（收口设计 §2.1 / §六 S1）。
//
// 为什么需要这一层：remote 包禁止 import main，否则"横向原语"会反过来认识
// 具体会话类型，四条链路就永远无法共用同一份写入/探测/拉取实现。适配是零逻辑
// 包装——只做端口对接，不做任何语义加工（语义差异由 remote/exec.go 的包装层
// 显式提供，见 S0 差异表）。
//
// 三个接口的实现关系：
//   - Runner：runQuickCommand（stdout 空时 stderr 兜底，历史契约）+ WriteStdin
//   - StreamsRunner：runQuickCommandStreams（分轨，区分 exit 127 与文件不存在）
//   - StreamRunner：ReadStream（流式拉取，20 GiB 包不进桥进程内存）

// sshRunner 把 *SSHSession 适配成 remote 认识的执行器。
type sshRunner struct {
	session *SSHSession
}

// Runner 是适配器对外暴露的完整能力（执行 + 分轨 + 流式）。
//
// 为什么要显式定义组合接口：remote 把三种能力拆成了三个窄接口（窄接口是对的），
// 但适配器同时实现全部；若构造函数只声明其中一个，调用方就得做类型断言才能拿到
// 另一种能力，反而制造噪音。
type Runner interface {
	remote.Runner
	remote.StreamsRunner
	remote.StreamRunner
	// 会话生命周期：长任务保护与保活（交付域与执行原语同属一个端口）。
	BeginLongTask()
	EndLongTask()
	StartShellHeartbeat(caseID string) func()
	MarkBusy()
	// RunCommandPty 在独立通道上分配伪终端执行（acli 命令需要 tty）。
	RunCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error)
}

// NewSSHRunner 构造适配器；session 为 nil 时返回 nil，由 remote 侧判空报错。
func NewSSHRunner(s *SSHSession) Runner {
	if s == nil {
		return nil
	}
	return sshRunner{session: s}
}

func (r sshRunner) RunQuickCommand(ctx context.Context, command string) (string, int, error) {
	return r.session.RunQuickCommand(ctx, command)
}

func (r sshRunner) RunQuickCommandStreams(ctx context.Context, command string) (string, string, int, error) {
	return r.session.RunQuickCommandStreams(ctx, command)
}

// WriteStdin 向远端命令的 stdin 流式写入，并捕获 stdout/stderr 与退出码。
//
// 与历史 uploadStream 的关键差异（后者已删除）：stdout 被捕获而不是丢弃，
// 否则远端"先校验再 mv"的结论无处可取，上游只能靠人类文案猜成败。
//
// 退出码约定：远端非零退出 → 该码；通道异常/ctx 取消 → -1（没有退出码可言）。
// ctx 取消时发 SIGKILL 终止远端进程，不留半包继续写。
func (r sshRunner) WriteStdin(ctx context.Context, command string, reader io.Reader) (string, int, error) {
	session, err := r.session.Client.NewSession()
	if err != nil {
		return "", -1, err
	}
	defer session.Close()

	var stdoutBuf, stderrBuf bytes.Buffer
	session.Stdout = &stdoutBuf
	session.Stderr = &stderrBuf
	session.Stdin = reader

	done := make(chan error, 1)
	go func() {
		done <- session.Run(command)
	}()

	select {
	case <-ctx.Done():
		_ = session.Signal(ssh.SIGKILL)
		return "", -1, ctx.Err()
	case err := <-done:
		exitCode := 0
		if err != nil {
			var exitErr *ssh.ExitError
			if errors.As(err, &exitErr) {
				exitCode = exitErr.ExitStatus()
			} else {
				exitCode = -1
			}
		}
		out := stdoutBuf.String()
		if out == "" {
			out = stderrBuf.String()
		}
		return out, exitCode, err
	}
}

// pullStream 是 ReadStream 返回的读端：Close 必须能终止远端命令。
//
// 只关本地管道不够——节点上的 cat/tail 会继续跑，白占带宽与文件句柄，
// 且下一次续传会与它争同一个源文件。
//
// finished 用 sync.Once 关闭：正常读完（wait）与异常中断（Close）两条路径
// 都会到达，只看门 goroutine 必须能被任一条唤醒，否则 ctx 为 Background 时
// 它会永远阻塞——每拉一次包泄漏一个 goroutine。
type pullStream struct {
	pipe     io.Reader
	session  *ssh.Session
	finished chan struct{}
	once     sync.Once
}

func (p *pullStream) Read(b []byte) (int, error) { return p.pipe.Read(b) }

func (p *pullStream) Close() error {
	p.once.Do(func() { close(p.finished) })
	_ = p.session.Signal(ssh.SIGKILL)
	return p.session.Close()
}

func (p *pullStream) Finish() {
	p.once.Do(func() { close(p.finished) })
	_ = p.session.Close()
}

// ReadStream 启动远端命令并返回其 stdout 流与等待函数。
//
// 调用方负责 Close（remote.Pull 用 defer 保证）；Wait 必须在读完后调用，
// 否则拿不到远端退出码，"源包被清理"会表现成一次静默的短读。
// 生命周期方法：交付域需要"长任务期间免于空闲回收 + 保活"的语义，
// 与执行原语同属一个端口，避免调用方同时持有两种对象。
func (r sshRunner) BeginLongTask() { r.session.BeginLongTask() }

func (r sshRunner) EndLongTask() { r.session.EndLongTask() }

func (r sshRunner) StartShellHeartbeat(caseID string) func() {
	return r.session.StartShellHeartbeat(caseID)
}

func (r sshRunner) MarkBusy() { r.session.MarkBusy() }

func (r sshRunner) RunCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error) {
	return r.session.RunCommandPty(ctx, command, timeout)
}

func (r sshRunner) ReadStream(ctx context.Context, command string) (io.ReadCloser, remote.WaitFunc, error) {
	session, err := r.session.Client.NewSession()
	if err != nil {
		return nil, nil, err
	}
	pipe, err := session.StdoutPipe()
	if err != nil {
		_ = session.Close()
		return nil, nil, err
	}
	if err := session.Start(command); err != nil {
		_ = session.Close()
		return nil, nil, err
	}
	stream := &pullStream{pipe: pipe, session: session, finished: make(chan struct{})}
	wait := func() error {
		err := session.Wait()
		stream.Finish()
		return err
	}
	// ctx 取消时主动终止：远端不会感知本地 context，必须显式发信号。
	go func() {
		select {
		case <-ctx.Done():
			_ = stream.Close()
		case <-stream.finished:
		}
	}()
	return stream, wait, nil
}

// 编译期断言：适配器必须同时满足三个接口，否则 remote 侧的可选能力会静默降级。
var (
	_ remote.Runner        = sshRunner{}
	_ remote.StreamsRunner = sshRunner{}
	_ remote.StreamRunner  = sshRunner{}
)
