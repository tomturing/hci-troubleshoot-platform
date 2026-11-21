package sshx

// pty.go —— 伪终端参数与 PTY 隔离执行。
//
// 为什么在 sshx 而不是 terminal：PTY 是 SSH 协议层细节（RequestPty 的参数），
// 而 terminal 依赖 sshx（单向），把常量与执行实现放在 sshx 才能让 delivery 这类
// 需要"跑一条 acli 命令"的域复用，而不必反向借 terminal/logcollect。

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

// 终端尺寸：高度/宽度只影响换行与列宽，不影响命令语义。
const (
	PtyTerminalHeight = 40
	PtyTerminalWidth  = 160
)

// PtyTerminalModes 关闭回显与输出换行转换：
//   - ECHO=0：避免输入被回显进 stdout；
//   - OPOST=0 / ONLCR=0：禁止把 \n 改写成 \r\n，从源头减少 CRLF。
func PtyTerminalModes() ssh.TerminalModes {
	return ssh.TerminalModes{
		ssh.ECHO:  0,
		ssh.OPOST: 0,
		ssh.ONLCR: 0,
	}
}

// NormalizePtyOutput 把 PTY 输出里的 CRLF 归一化为 LF（幂等）。
func NormalizePtyOutput(s string) string { return strings.ReplaceAll(s, "\r\n", "\n") }

// RunCommandPty 在独立 Session 上分配伪终端执行命令并返回合并输出。
// 超时后强制关闭 Session（仅发信号在部分 sshd 上不足以终止容器内进程）。
//
// 为什么必须独立 Session：共享交互式 shell 上跑 PTY 命令会污染用户终端，
// 且超时时无法确定性中止——关独立连接才能让远端进程立刻退出。
func (s *SSHSession) RunCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error) {
	if s == nil || s.Client == nil {
		return "", -1, fmt.Errorf("no-session")
	}
	sess, err := s.Client.NewSession()
	if err != nil {
		return "", -1, err
	}
	defer sess.Close()

	if err := sess.RequestPty("xterm-256color", PtyTerminalHeight, PtyTerminalWidth, PtyTerminalModes()); err != nil {
		return "", -1, fmt.Errorf("request pty: %w", err)
	}

	type execOutcome struct {
		output []byte
		err    error
	}
	outcomeCh := make(chan execOutcome, 1)
	go func() {
		output, runErr := sess.CombinedOutput(command)
		outcomeCh <- execOutcome{output: output, err: runErr}
	}()

	execCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	select {
	case <-execCtx.Done():
		_ = sess.Signal(ssh.SIGKILL)
		_ = sess.Close()
		return "", -1, execCtx.Err()
	case outcome := <-outcomeCh:
		exitCode := 0
		if outcome.err != nil {
			var exitErr *ssh.ExitError
			if errors.As(outcome.err, &exitErr) {
				exitCode = exitErr.ExitStatus()
			} else {
				exitCode = -1
			}
		}
		return NormalizePtyOutput(string(outcome.output)), exitCode, nil
	}
}
