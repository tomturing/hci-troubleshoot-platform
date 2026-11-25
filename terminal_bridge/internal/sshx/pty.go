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
// ErrNoSession 表示会话已失效（Client 为 nil）。
//
// 用哨兵而不是临时字符串：交付域据此立即判定"会话不可用并交回重排"，
// 既不必累计 3 次失败，也不必靠错误文案匹配。
var ErrNoSession = errors.New("no-session")

// Alive 报告会话是否仍可执行命令。
//
// 为什么判 Client 而不是只看指针非空：Close() 会在锁内置空 Client，但会话
// 对象可能仍留在 hub 注册表里（DestroySession 摘表与 Close 是两条路径）。
// 只看指针会把"已关闭但未摘表"的僵尸会话当成可用——交付每轮都拿到 no-session。
// 2026-11-25 Q2026112500592：主节点会话销毁后，only_session_for_case 兜底
// 返回另一台机器的僵尸会话，连续 5 轮全部 no-session 直至失败。
func (s *SSHSession) Alive() bool {
	if s == nil {
		return false
	}
	s.Mu.Lock()
	defer s.Mu.Unlock()
	return s.Client != nil
}

func (s *SSHSession) RunCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error) {
	if s == nil {
		return "", -1, ErrNoSession
	}
	s.Mu.Lock()
	client := s.Client
	s.Mu.Unlock()
	if client == nil {
		return "", -1, ErrNoSession
	}

	// 建链（NewSession + RequestPty）必须与命令执行共享同一个超时预算。
	//
	// 旧实现只给 CombinedOutput 加了超时，建链两步裸奔：SSH 通道半开时它们
	// 可以永久阻塞，表现为"命令根本没开始跑，整轮却 20+ 分钟不动"。
	// Q2026112500592 的 07:28→07:49 跳变即由此放大。
	execCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	type ptySetup struct {
		sess *ssh.Session
		err  error
	}
	setupCh := make(chan ptySetup, 1)
	go func() {
		sess, err := client.NewSession()
		if err != nil {
			setupCh <- ptySetup{err: err}
			return
		}
		if err := sess.RequestPty("xterm-256color", PtyTerminalHeight, PtyTerminalWidth, PtyTerminalModes()); err != nil {
			_ = sess.Close()
			setupCh <- ptySetup{err: fmt.Errorf("request pty: %w", err)}
			return
		}
		setupCh <- ptySetup{sess: sess}
	}()

	var sess *ssh.Session
	select {
	case <-execCtx.Done():
		// 建链可能已经成功但结果还没送到，非阻塞收一次并关闭，避免泄漏通道。
		select {
		case setup := <-setupCh:
			if setup.sess != nil {
				_ = setup.sess.Close()
			}
		default:
		}
		return "", -1, execCtx.Err()
	case setup := <-setupCh:
		if setup.err != nil {
			return "", -1, setup.err
		}
		sess = setup.sess
	}
	defer sess.Close()

	type execOutcome struct {
		output []byte
		err    error
	}
	outcomeCh := make(chan execOutcome, 1)
	go func() {
		output, runErr := sess.CombinedOutput(command)
		outcomeCh <- execOutcome{output: output, err: runErr}
	}()

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
