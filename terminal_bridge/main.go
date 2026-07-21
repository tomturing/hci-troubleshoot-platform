// terminal_bridge - HCI 排障助手本地 SSH Bridge
// 架构: Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
// 编译: 执行 build_windows.bat 即可
// 体积: ~3-4MB 原生，upx 压缩后 ~1.5MB，支持 Win7/10/11，无任何运行时依赖

package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"sync/atomic"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"runtime/debug"

	"golang.org/x/crypto/ssh"
	"golang.org/x/net/websocket"
)

const (
	wsPort = 9999
)

// ── 消息结构 ─────────────────────────────────────────────────────────────────

type InMessage struct {
	Type       string `json:"type"`
	CaseID     string `json:"case_id"`
	Host       string `json:"host"`
	Username   string `json:"username"`
	Port       int    `json:"port"`
	AuthType   string `json:"auth_type"`
	Password   string `json:"password"`
	PrivateKey string `json:"private_key"`
	Passphrase string `json:"passphrase"`
	Data       string `json:"data"`
	Command    string `json:"command"`
	ExecID     string `json:"exec_id"`   // 用于 ssh_exec_command 和 ssh_exec_process
	NodeIP     string `json:"node_ip"`   // 目标节点 IP（多节点路由）
	Container  string `json:"container"` // 目标容器名（空或"host"=物理机直连）
	TraceID    string `json:"trace_id"`  // 端到端链路追踪 ID（Custom-UI → Bridge → Agent 统一）
	Resume     bool   `json:"resume"`     // P0-2: 浏览器重连时发送 resume 信号，触发历史日志回放  // 端到端链路追踪 ID（Custom-UI → Bridge → Agent 统一）
}

type OutMessage struct {
	Type     string `json:"type"`
	CaseID   string `json:"case_id"`
	Output   string `json:"output,omitempty"`
	Message  string `json:"message,omitempty"`
	Detail   string `json:"detail,omitempty"`
	ExecID   string `json:"exec_id,omitempty"`   // 用于 exec_result
	ExitCode int    `json:"exit_code,omitempty"` // 用于 exec_result
	Stdout   string `json:"stdout,omitempty"`    // 双通道物理隔离输出 (Scheme B)
	Stderr   string `json:"stderr,omitempty"`    // 双通道物理隔离输出 (Scheme B)
	TraceID  string `json:"trace_id,omitempty"`  // 回显端到端 trace_id
	CustomUI string `json:"custom_ui,omitempty"` // 来源 Custom-UI（自动按 Origin 关联）
}

// ── Exec Marker 监听器 ─────────────────────────────────────────────────────────

// ExecListener 用于追踪命令执行的 marker
type ExecListener struct {
	ExecID     string           // 执行 ID
	StartTime  time.Time        // 开始时间
	OutputBuf  *strings.Builder // 输出缓冲区
	ResultChan chan ExecResult  // 结果通道
}

// ExecResult 命令执行结果
type ExecResult struct {
	Output   string
	ExitCode int
	Timeout  bool
}

// ── SSH 会话 ──────────────────────────────────────────────────────────────────

type SSHSession struct {
	caseID      string
	client      *ssh.Client
	session     *ssh.Session
	stdin       io.WriteCloser
	mu          sync.Mutex
	closed      bool
	listenersMu sync.Mutex
	listeners   map[string]*ExecListener // key: execID
}

func newSSHSession(msg InMessage) (*SSHSession, error) {
	if strings.TrimSpace(msg.Host) == "" {
		return nil, fmt.Errorf("主机地址不能为空")
	}
	if strings.TrimSpace(msg.Username) == "" {
		return nil, fmt.Errorf("用户名不能为空")
	}

	// 1. 用户名转换：前端输入如果是 admin，后端实际登录用 root
	username := strings.TrimSpace(msg.Username)
	if username == "admin" {
		username = "root"
	}
	msg.Username = username

	// 2. 密码后缀处理：如果是密码认证且提供了密码，加后缀 sangfornetwork
	authType := strings.TrimSpace(strings.ToLower(msg.AuthType))
	if (authType == "password" || authType == "") && msg.Password != "" {
		msg.Password = msg.Password + "sangfornetwork"
	}

	port := msg.Port
	if port == 0 {
		port = 22
	}

	authMethods, err := buildAuthMethods(msg)
	if err != nil {
		return nil, err
	}

	clientConfig := &ssh.ClientConfig{
		User:            username,
		Auth:            authMethods,
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         12 * time.Second,
	}
	addr := fmt.Sprintf("%s:%d", strings.TrimSpace(msg.Host), port)

	client, err := ssh.Dial("tcp", addr, clientConfig)
	if err != nil {
		return nil, fmt.Errorf("建立 SSH 连接失败: %w", err)
	}
	session, err := client.NewSession()
	if err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("创建 SSH 会话失败: %w", err)
	}

	return &SSHSession{
		caseID:    msg.CaseID,
		client:    client,
		session:   session,
		listeners: make(map[string]*ExecListener),
	}, nil
}

func buildAuthMethods(msg InMessage) ([]ssh.AuthMethod, error) {
	authType := strings.TrimSpace(strings.ToLower(msg.AuthType))
	methods := make([]ssh.AuthMethod, 0, 2)

	if authType == "password" || authType == "" {
		if strings.TrimSpace(msg.Password) == "" {
			return nil, fmt.Errorf("密码不能为空")
		}
		password := msg.Password
		methods = append(methods, ssh.Password(password))
		methods = append(methods, ssh.KeyboardInteractive(func(user, instruction string, questions []string, echos []bool) ([]string, error) {
			answers := make([]string, len(questions))
			for i := range questions {
				answers[i] = password
			}
			return answers, nil
		}))
		return methods, nil
	}

	if authType == "key" {
		if strings.TrimSpace(msg.PrivateKey) == "" {
			return nil, fmt.Errorf("私钥不能为空")
		}
		var signer ssh.Signer
		var err error
		if msg.Passphrase != "" {
			signer, err = ssh.ParsePrivateKeyWithPassphrase([]byte(msg.PrivateKey), []byte(msg.Passphrase))
		} else {
			signer, err = ssh.ParsePrivateKey([]byte(msg.PrivateKey))
		}
		if err != nil {
			return nil, fmt.Errorf("私钥解析失败: %w", err)
		}
		methods = append(methods, ssh.PublicKeys(signer))
		return methods, nil
	}

	return nil, fmt.Errorf("不支持的认证方式: %s", msg.AuthType)
}

func (s *SSHSession) start() (io.ReadCloser, error) {
	stdin, err := s.session.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("获取 SSH stdin 失败: %w", err)
	}
	stdout, err := s.session.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("获取 SSH stdout 失败: %w", err)
	}
	stderr, err := s.session.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("获取 SSH stderr 失败: %w", err)
	}

	modes := ssh.TerminalModes{
		ssh.ECHO:          1,
		ssh.TTY_OP_ISPEED: 14400,
		ssh.TTY_OP_OSPEED: 14400,
	}
	if err := s.session.RequestPty("xterm-256color", 40, 160, modes); err != nil {
		return nil, fmt.Errorf("申请远端 PTY 失败: %w", err)
	}
	if err := s.session.Shell(); err != nil {
		return nil, fmt.Errorf("启动远端 shell 失败: %w", err)
	}

	s.mu.Lock()
	s.stdin = stdin
	s.mu.Unlock()

	pipeReader, pipeWriter := io.Pipe()
	var wg sync.WaitGroup
	forward := func(name string, r io.Reader) {
		defer wg.Done()
		if _, copyErr := io.Copy(pipeWriter, r); copyErr != nil && !errors.Is(copyErr, io.ErrClosedPipe) {
			log.Printf("[Bridge] SSH 输出转发异常: case=%s stream=%s err=%v", s.caseID, name, copyErr)
		}
	}

	wg.Add(2)
	go forward("stdout", stdout)
	go forward("stderr", stderr)
	go func() {
		wg.Wait()
		_ = pipeWriter.Close()
	}()

	return pipeReader, nil
}

func (s *SSHSession) send(data string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stdin != nil {
		if _, err := io.WriteString(s.stdin, data); err != nil {
			log.Printf("[Bridge] SSH 输入写入失败: case=%s err=%v", s.caseID, err)
		}
	}
}

// injectCommand 填入命令行但不加换行，等客户回车确认
func (s *SSHSession) injectCommand(command string) {
	s.send(command)
}

// ── Exec Marker 监听器管理 ───────────────────────────────────────────────────

func (s *SSHSession) registerExecListener(listener *ExecListener) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	s.listeners[listener.ExecID] = listener
}

func (s *SSHSession) unregisterExecListener(execID string) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	delete(s.listeners, execID)
}

func (s *SSHSession) appendOutput(chunk string) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	for _, listener := range s.listeners {
		listener.OutputBuf.WriteString(chunk)
	}
}

func (s *SSHSession) checkMarkers(output string) (*ExecListener, int, bool) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()

	for execID, listener := range s.listeners {
		normalizedExecID := strings.ReplaceAll(execID, "-", "")
		if len(normalizedExecID) < 16 {
			continue
		}
		markerPrefix := "__EXEC_DONE_" + normalizedExecID[:16] + ":"
		if idx := strings.Index(output, markerPrefix); idx != -1 {
			markerStart := idx
			markerEnd := strings.IndexByte(output[markerStart:], '\n')
			if markerEnd == -1 {
				markerEnd = len(output) - markerStart
			}
			markerLine := output[markerStart : markerStart+markerEnd]

			var exitCode int
			if _, err := fmt.Sscanf(markerLine, markerPrefix+"%d", &exitCode); err != nil {
				if strings.Contains(markerLine, "%s") || strings.Contains(markerLine, "$status") {
					continue
				}
				exitCode = -1
			}
			return listener, exitCode, true
		}
	}
	return nil, 0, false
}

func (s *SSHSession) execCommand(command, execID string, timeout time.Duration) <-chan ExecResult {
	resultChan := make(chan ExecResult, 1)

	listener := &ExecListener{
		ExecID:     execID,
		StartTime:  time.Now(),
		OutputBuf:  &strings.Builder{},
		ResultChan: resultChan,
	}

	s.registerExecListener(listener)

	go func() {
		select {
		case <-time.After(timeout):
			s.listenersMu.Lock()
			if l, ok := s.listeners[execID]; ok {
				output := l.OutputBuf.String()
				delete(s.listeners, execID)
				s.listenersMu.Unlock()
				resultChan <- ExecResult{Output: output, ExitCode: -1, Timeout: true}
			} else {
				s.listenersMu.Unlock()
			}
		case <-resultChan:
		}
	}()

	s.send(command)

	return resultChan
}

// execCommandIsolated 独立建立 SSH Session 执行命令 (双通道 - 事务执行设计)
func (s *SSHSession) execCommandIsolated(ws *websocket.Conn, command, execID string) {
	startTime := time.Now()

	// P0: 记录命令开始
	blog("INFO", "exec.start", "开始执行命令", "", s.caseID, "", "", map[string]any{
		"exec_id":     execID,
		"command":     command,
		"command_len": len(command),
	})

	session, err := s.client.NewSession()
	if err != nil {
		// P0: 记录错误（包含详细错误信息和分类）
		blog("ERROR", "exec.error", "创建隔离 SSH 会话失败", "", s.caseID, "", "", map[string]any{
			"exec_id":    execID,
			"error":      err.Error(),
			"error_type": "session_creation_failed",
		})
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("创建隔离 SSH 会话失败: %v", err), ExitCode: -1,
		})
		return
	}
	defer session.Close()

	stdoutPipe, err := session.StdoutPipe()
	if err != nil {
		blog("ERROR", "exec.error", "获取 StdoutPipe 失败", "", s.caseID, "", "", map[string]any{
			"exec_id":    execID,
			"error":      err.Error(),
			"error_type": "stdout_pipe_failed",
		})
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("获取 StdoutPipe 失败: %v", err), ExitCode: -1,
		})
		return
	}
	stderrPipe, err := session.StderrPipe()
	if err != nil {
		blog("ERROR", "exec.error", "获取 StderrPipe 失败", "", s.caseID, "", "", map[string]any{
			"exec_id":    execID,
			"error":      err.Error(),
			"error_type": "stderr_pipe_failed",
		})
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("获取 StderrPipe 失败: %v", err), ExitCode: -1,
		})
		return
	}

	if err := session.Start(command); err != nil {
		blog("ERROR", "exec.error", "启动命令失败", "", s.caseID, "", "", map[string]any{
			"exec_id":    execID,
			"command":    command,
			"error":      err.Error(),
			"error_type": "command_start_failed",
		})
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("启动命令失败: %v", err), ExitCode: -1,
		})
		return
	}

	var stdoutBuf strings.Builder
	var stderrBuf strings.Builder
	var wg sync.WaitGroup

	wg.Add(2)
	go func() {
		defer wg.Done()
		buf := make([]byte, 4096)
		for {
			n, rerr := stdoutPipe.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				stdoutBuf.WriteString(chunk)
				sendMsg(ws, OutMessage{Type: "exec_stdout", CaseID: s.caseID, ExecID: execID, Stdout: chunk})
			}
			if rerr != nil {
				break
			}
		}
	}()

	go func() {
		defer wg.Done()
		buf := make([]byte, 4096)
		for {
			n, rerr := stderrPipe.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				stderrBuf.WriteString(chunk)
				sendMsg(ws, OutMessage{Type: "exec_stderr", CaseID: s.caseID, ExecID: execID, Stderr: chunk})
			}
			if rerr != nil {
				break
			}
		}
	}()

	wg.Wait()

	exitCode := 0
	if werr := session.Wait(); werr != nil {
		if exitErr, ok := werr.(*ssh.ExitError); ok {
			exitCode = exitErr.ExitStatus()
		} else {
			exitCode = -1
		}
	}

	duration := time.Since(startTime)

	// P0: 记录命令完成（包含所有关键信息）
	outputPreview := stdoutBuf.String()
	if len(outputPreview) > 500 {
		outputPreview = outputPreview[:500] + "...(截断)"
	}

	blog("INFO", "exec.done", "命令执行完成", "", s.caseID, "", "", map[string]any{
		"exec_id":        execID,
		"command":        command,
		"exit_code":      exitCode,
		"success":        exitCode == 0,
		"duration_ms":    duration.Milliseconds(),
		"stdout_len":     stdoutBuf.Len(),
		"stderr_len":     stderrBuf.Len(),
		"output_preview": outputPreview,
	})

	sendMsg(ws, OutMessage{
		Type: "exec_result", CaseID: s.caseID, ExecID: execID,
		Stdout: stdoutBuf.String(), Stderr: stderrBuf.String(), ExitCode: exitCode,
	})
}

func summarizeSSHDetail(output string) string {
	cleaned := strings.ReplaceAll(output, "\r", "")
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return ""
	}
	lines := strings.Split(cleaned, "\n")
	if len(lines) > 8 {
		lines = lines[len(lines)-8:]
	}
	compact := strings.TrimSpace(strings.Join(lines, "\n"))
	if len(compact) > 600 {
		compact = compact[len(compact)-600:]
	}
	return compact
}

func classifySSHFailure(output string) (string, string, bool) {
	text := strings.ToLower(output)
	detail := summarizeSSHDetail(output)
	failures := []struct {
		patterns []string
		message  string
	}{
		{[]string{"could not resolve hostname", "name or service not known", "temporary failure in name resolution"}, "主机地址无法解析"},
		{[]string{"no route to host", "network is unreachable"}, "无法到达目标主机"},
		{[]string{"connection refused"}, "目标主机拒绝连接"},
		{[]string{"connection timed out", "operation timed out"}, "连接远程主机超时"},
		{[]string{"connection reset by peer"}, "连接被远端重置"},
		{[]string{"kex_exchange_identification", "banner exchange", "handshake failed"}, "SSH 握手失败"},
		{[]string{"connection closed by remote host"}, "远端主机主动关闭了连接"},
		{[]string{"host key verification failed"}, "主机指纹校验失败"},
		{[]string{"permission denied (publickey)", "sign_and_send_pubkey", "load key", "invalid format", "error in libcrypto"}, "私钥认证失败"},
		{[]string{"enter passphrase for key", "bad passphrase", "incorrect passphrase"}, "当前不支持带口令的私钥或口令错误"},
		{[]string{"no supported authentication methods available"}, "目标主机不接受当前认证方式"},
		{[]string{"verification code", "one-time password", "keyboard-interactive", "mfa", "duo two-factor", "otp"}, "当前不支持多因素认证"},
		{[]string{"account is locked", "account locked", "account is disabled", "user not allowed", "not allowed because"}, "账号不可用"},
		{[]string{"password expired", "must change your password", "change of password required"}, "账号密码已过期，当前不支持改密流程"},
		{[]string{"this account is currently not available"}, "登录成功但账号无可用 Shell"},
		{[]string{"too many authentication failures"}, "认证失败次数过多"},
		{[]string{"permission denied", "authentication failed", "access denied"}, "用户名或密码错误"},
	}

	for _, failure := range failures {
		for _, pattern := range failure.patterns {
			if strings.Contains(text, pattern) {
				return failure.message, detail, true
			}
		}
	}
	return "", detail, false
}

func (s *SSHSession) on_output_start(
	ws *websocket.Conn,
	stdout io.ReadCloser,
	caseID string,
	onExit func(),
) {
	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := stdout.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				s.appendOutput(chunk)

				if listener, exitCode, matched := s.checkMarkers(chunk); matched {
					output := listener.OutputBuf.String()
					normalizedExecID := strings.ReplaceAll(listener.ExecID, "-", "")
					if len(normalizedExecID) >= 16 {
						markerPrefix := "__EXEC_DONE_" + normalizedExecID[:16] + ":"
						re := regexp.MustCompile(regexp.QuoteMeta(markerPrefix) + `(-?\d+)`)
						loc := re.FindStringIndex(output)
						if loc != nil {
							output = output[:loc[0]]
						} else if idx := strings.Index(output, markerPrefix); idx != -1 {
							output = output[:idx]
						}
						echoPlaceholder := markerPrefix + "%s"
						if echoIdx := strings.Index(output, echoPlaceholder); echoIdx != -1 {
							remaining := output[echoIdx:]
							if nl1 := strings.Index(remaining, "\n"); nl1 != -1 {
								remaining2 := remaining[nl1+1:]
								if nl2 := strings.Index(remaining2, "\n"); nl2 != -1 {
									output = remaining2[nl2+1:]
								} else {
									output = remaining2
								}
							}
						}
						output = strings.TrimSpace(output)
					}

					sendMsg(ws, OutMessage{
						Type: "exec_result", CaseID: caseID, ExecID: listener.ExecID,
						Output: output, ExitCode: exitCode,
					})
					s.unregisterExecListener(listener.ExecID)
					select {
					case listener.ResultChan <- ExecResult{Output: output, ExitCode: exitCode}:
					default:
					}
				}

				sendMsg(ws, OutMessage{Type: "ssh_output", CaseID: caseID, Output: chunk})

					// 缺陷 2: 回采 SSH 终端交互日志（用户手动输入/输出的关键信息）
					// 只回采有意义的内容（过滤空行和控制序列）
					if len(chunk) > 0 && chunk != "\r\n" && !strings.Contains(chunk, "\x1b[") {
						blog("INFO", "ssh.output", "SSH 终端输出", "", caseID, "", "", map[string]any{
							"output_len":     len(chunk),
							"output_preview": truncateString(chunk, 200),
						})
					}
			}
			if err != nil {
				if err != io.EOF {
					log.Printf("[Bridge] SSH 输出读取异常: case=%s err=%v", caseID, err)
				}
				break
			}
		}

		if err := s.wait(); err != nil && !s.isClosed() {
			log.Printf("[Bridge] SSH 会话退出(异常): case=%s err=%v", caseID, err)
		}
		sendMsg(ws, OutMessage{Type: "ssh_disconnected", CaseID: caseID})
		onExit()
	}()
}

func (s *SSHSession) wait() error {
	s.mu.Lock()
	session := s.session
	s.mu.Unlock()
	if session == nil {
		return nil
	}
	return session.Wait()
}

func (s *SSHSession) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

func (s *SSHSession) close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	stdin := s.stdin
	s.stdin = nil
	session := s.session
	s.session = nil
	client := s.client
	s.client = nil
	s.mu.Unlock()

	if stdin != nil {
		_ = stdin.Close()
	}
	if session != nil {
		_ = session.Close()
	}
	if client != nil {
		_ = client.Close()
	}
}

func buildSSHError(err error) (string, string) {
	detail := summarizeSSHDetail(err.Error())
	if msg, _, matched := classifySSHFailure(err.Error()); matched {
		return msg, detail
	}
	return "SSH 连接失败", detail
}

// ── 容器命令包装 ─────────────────────────────────────────────────────────────

// wrapContainerCommand 根据 container 类型包装命令
// container="host" 或空 → 原样执行
// container="vs-cp-manager" 等 → container_exec -n <container> -c "<command>"
func wrapContainerCommand(command, container string) string {
	container = strings.TrimSpace(container)
	if container == "" || container == "host" {
		return command
	}
	// 对命令中的单引号做转义
	escaped := strings.ReplaceAll(command, "'", "'\\''")
	return fmt.Sprintf("container_exec -n %s -c '%s'", container, escaped)
}

// ── 结构化日志与回采（Observability）────────────────────────────────────────
//
// 设计目标（第一性原理 + 业界范式）：
//   1. 统一可观测性：所有日志结构化为 JSON，携带 trace_id / case_id / node_ip /
//      custom_ui 标签，接入平台整体链路（Custom-UI → Bridge → Agent 共享同一 trace）。
//   2. 回采：日志经 WebSocket 以 `bridge_log` 消息实时推给已连接的 Custom-UI 浏览器，
//      由前端统一 POST 到其后台「回采接口」落库（工单关联）。Bridge 为通用代理，
//      不感知后台地址，只按连接 Origin 自动归属 custom_ui。
//   3. 多工单并行/串行：会话本就以 caseID@nodeIP 为键；日志按 case_id 归属与回放。
//   4. 异常重传：环形缓冲保留近期日志，浏览器（重）连接并 ssh_connect 时回放；
//      发送失败暂存 pending，连接恢复后重传；可选落本地文件供进程重启回放。

type logEntry struct {
	Type      string         `json:"type"` // bridge_log - 前端监听 type==="bridge_log" 的必备字段
	Seq       uint64         `json:"seq"`
	Timestamp string         `json:"ts"`
	Level     string         `json:"level"`
	Service   string         `json:"service"`
	Event     string         `json:"event,omitempty"`
	Message   string         `json:"message,omitempty"`
	TraceID   string         `json:"trace_id,omitempty"`
	CaseID    string         `json:"case_id,omitempty"`
	NodeIP    string         `json:"node_ip,omitempty"`
	CustomUI  string         `json:"custom_ui,omitempty"`
	Extra        map[string]any `json:"extra,omitempty"`
	Traceparent string         `json:"traceparent,omitempty"` // P2-7: W3C traceparent 标准格式
	// 已知限制：当前只支持 Trace ID 链路追踪，未建立完整的 Span 父子关系
	// 完整实现需要：1) 生成唯一 Span ID；2) 维护 Parent Span ID；3) 建立 Span 栈管理
	// 当前方案：Traceparent 格式为 "00-{trace_id}-0000000000000001-01"，Span ID 固定为 1
	// 未来优化：引入 OpenTelemetry SDK 或实现 Span 管理器
}

// bridgeSubscriber 代表一个已连接的 Custom-UI 浏览器（一个回采订阅者）。
type bridgeSubscriber struct {
	conn     *websocket.Conn
	customUI string
	caseID   string
	mu       sync.Mutex
	pending  []logEntry // 断网/重启期间的待重传缓冲（有界）
}

// LogHub 全局日志中枢：结构化落盘 + 环形缓冲 + 多订阅者回采。
type LogHub struct {
	mu      sync.Mutex
	seq     uint64
	cap     int
	ring    []logEntry // 全局有界环形缓冲，供晚加入/重连回放
	subs    map[*websocket.Conn]*bridgeSubscriber
	logFile *os.File
}

var logHub = newLogHub()

func newLogHub() *LogHub {
	h := &LogHub{
		cap:  5000,
		subs: make(map[*websocket.Conn]*bridgeSubscriber),
	}
	// 本地持久化（重启回放）：best-effort，受 HCI_BRIDGE_LOG_DIR 控制。
	if dir := os.Getenv("HCI_BRIDGE_LOG_DIR"); dir != "" {
		_ = os.MkdirAll(dir, 0o755)
		logPath := filepath.Join(dir, "bridge.log")
		if f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err == nil {
			h.logFile = f
		}
		
		// P0-3: 进程重启时回放本地日志文件
		if replayFile, err := os.Open(logPath); err == nil {
			defer replayFile.Close()
			scanner := bufio.NewScanner(replayFile)
			replayCount := 0
			for scanner.Scan() {
				line := scanner.Text()
				var entry logEntry
				if err := json.Unmarshal([]byte(line), &entry); err == nil {
					h.ring = append(h.ring, entry)
					replayCount++
				}
			}
			if replayCount > 0 {
				log.Printf("[Bridge] 进程重启回放: 从 %s 加载 %d 条历史日志", logPath, replayCount)
			}
		}
	}
	return h
}


func (h *LogHub) publish(e logEntry) {
	h.mu.Lock()
	h.seq++
	e.Seq = h.seq
	e.Timestamp = time.Now().UTC().Format(time.RFC3339Nano)
	e.Type = "bridge_log" // 前端监听 type==="bridge_log" 的必备字段，统一在此注入
	if len(h.ring) >= h.cap {
		h.ring = h.ring[1:]
	}
	h.ring = append(h.ring, e)
	if h.logFile != nil {
		if b, err := json.Marshal(e); err == nil {
			_, _ = h.logFile.Write(append(b, '\n'))
		}
	}
	for _, sub := range h.subs {
		if e.CaseID == "" || sub.caseID == "" || sub.caseID == e.CaseID {
			h.enqueue(sub, e)
		}
	}
	h.mu.Unlock()
	
	// P2-8: 更新 Prometheus 指标
	atomic.AddUint64(&promMetrics.LogsCollectedTotal, 1)
}

func (h *LogHub) enqueue(sub *bridgeSubscriber, e logEntry) {
	sub.mu.Lock()
	defer sub.mu.Unlock()
	if len(sub.pending) >= 2000 {
		sub.pending = sub.pending[1:]
	}
	sub.pending = append(sub.pending, e)
}

// flushSubscriber 把待重传缓冲经 bridge_log 推给浏览器；连接断开则保留待下次重传。
func (h *LogHub) flushSubscriber(sub *bridgeSubscriber) {
	sub.mu.Lock()
	pending := sub.pending
	sub.pending = nil
	sub.mu.Unlock()
	for _, e := range pending {
		e.CustomUI = sub.customUI // 按订阅者归属 custom_ui，自动回采到对应后台
		b, err := json.Marshal(e)
		if err != nil {
			continue
		}
		if err := websocket.Message.Send(sub.conn, string(b)); err != nil {
			sub.mu.Lock()
			if len(sub.pending) < 2000 {
				sub.pending = append(sub.pending, e)
			}
			sub.mu.Unlock()
			break
		}
	}
}

func (h *LogHub) addSubscriber(conn *websocket.Conn, customUI string) *bridgeSubscriber {
	sub := &bridgeSubscriber{conn: conn, customUI: customUI}
	h.mu.Lock()
	h.subs[conn] = sub
	h.mu.Unlock()
	return sub
}

// setCase 标记订阅者归属工单，并回放该工单近期日志（重连/晚加入可重传）。
func (h *LogHub) setCase(sub *bridgeSubscriber, caseID string) {
	h.mu.Lock()
	sub.caseID = caseID
	var replay []logEntry
	if caseID != "" {
		for _, e := range h.ring {
			if e.CaseID == caseID {
				replay = append(replay, e)
			}
		}
	}
	h.mu.Unlock()
	
	// P2-8: 更新回放指标
	if len(replay) > 0 {
		atomic.AddUint64(&promMetrics.LogsReplayedTotal, uint64(len(replay)))
	}
	
	for _, e := range replay {
		h.enqueue(sub, e)
	}
	go h.flushSubscriber(sub)
}

func (h *LogHub) removeSubscriber(conn *websocket.Conn) {
	h.mu.Lock()
	delete(h.subs, conn)
	h.mu.Unlock()
}

// bridgeLogWriter 把标准库 log 输出重定向为结构化日志并回采，从而零改造捕获全部既有 log.Printf。
type bridgeLogWriter struct{}

func (bridgeLogWriter) Write(p []byte) (int, error) {
	line := strings.TrimRight(string(p), "\n")
	level := "INFO"
	upper := strings.ToUpper(line)
	switch {
	case strings.Contains(upper, "ERROR"):
		level = "ERROR"
	case strings.Contains(upper, "WARNING"), strings.Contains(upper, "WARN"):
		level = "WARNING"
	}
	event := "bridge.log"
	msg := line
	if idx := strings.Index(line, "[Bridge] "); idx >= 0 {
		rest := line[idx+len("[Bridge] "):]
		if sp := strings.Index(rest, ":"); sp > 0 && sp < 48 {
			event = "bridge." + strings.ReplaceAll(strings.TrimSpace(rest[:sp]), " ", "_")
			msg = strings.TrimSpace(rest[sp+1:])
		}
	}
	logHub.publish(logEntry{Level: level, Service: "terminal_bridge", Event: event, Message: msg})
	return os.Stdout.Write(p) // 同时保留控制台原文，便于本地调试
}

// blog 记录带上下文（trace/case/node/custom_ui）的结构化日志并回采。
func blog(level, event, msg, traceID, caseID, nodeIP, customUI string, extra map[string]any) {
	// P2-7: 构造 W3C traceparent 格式（如果 traceID 存在）
	traceparent := ""
	if traceID != "" && traceID != "unknown" {
		// 格式：version-trace-id-parent-id-trace-flags
		// 简化实现：使用 traceID 作为 trace-id 部分
		traceparent = "00-" + traceID + "-" + "0000000000000001-01"
	}

	e := logEntry{
		Level:        level,
		Service:      "terminal_bridge",
		Event:        event,
		Message:      msg,
		TraceID:      traceID,
		CaseID:       caseID,
		NodeIP:       nodeIP,
		CustomUI:     customUI,
		Extra:        extra,
		Traceparent:  traceparent,
	}
	logHub.publish(e)
	if b, err := json.Marshal(e); err == nil {
		os.Stdout.Write(append(b, '\n'))
	}
}


// ── WebSocket Handler ─────────────────────────────────────────────────────────

// sessionKey 生成多节点会话的唯一键
func sessionKey(caseID, nodeIP string) string {
	if nodeIP == "" {
		return caseID
	}
	return caseID + "@" + nodeIP
}

type Bridge struct {
	mu       sync.Mutex
	sessions map[string]*SSHSession // key: caseID@nodeIP
	// 保存每个 caseID 最近一次 ssh_connect 的认证信息，用于自动连接新节点
	lastAuth map[string]InMessage // key: caseID
}

func newBridge() *Bridge {
	return &Bridge{
		sessions: make(map[string]*SSHSession),
		lastAuth: make(map[string]InMessage),
	}
}

func (b *Bridge) get(key string) *SSHSession {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.sessions[key]
}

func (b *Bridge) set(key string, s *SSHSession) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.sessions[key] = s
}

func (b *Bridge) remove(key string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.sessions, key)
}

// resolveSession 根据 caseID + nodeIP 解析 SSH 会话
// 有 nodeIP 时只精确匹配，不 fallback 到默认会话（避免命令在错误节点执行）
func (b *Bridge) resolveSession(msg InMessage) (*SSHSession, string) {
	if msg.NodeIP != "" {
		key := sessionKey(msg.CaseID, msg.NodeIP)
		if s := b.get(key); s != nil {
			log.Printf("[Bridge] resolveSession: 精确匹配 key=%s", key)
			return s, key
		}
		// 有 nodeIP 但找不到匹配会话 → 返回 nil，由调用方触发 autoConnect
		log.Printf("[Bridge] resolveSession: nodeIP=%s 无匹配会话，需自动连接", msg.NodeIP)
		return nil, ""
	}

	// 无 nodeIP → 使用默认会话
	defaultKey := sessionKey(msg.CaseID, "")
	if s := b.get(defaultKey); s != nil {
		log.Printf("[Bridge] resolveSession: 使用默认会话 key=%s", defaultKey)
		return s, defaultKey
	}
	return nil, ""
}

// autoConnectNode 使用已保存的认证信息自动连接新节点
func (b *Bridge) autoConnectNode(ws *websocket.Conn, msg InMessage) *SSHSession {
	if msg.NodeIP == "" {
		log.Printf("[Bridge] autoConnect: nodeIP 为空，跳过")
		return nil
	}

	auth, ok := b.lastAuth[msg.CaseID]
	if !ok {
		log.Printf("[Bridge] autoConnect: case=%s 缺少认证信息 (lastAuth 中无此 caseID)", msg.CaseID)
		sendMsg(ws, OutMessage{
			Type: "ssh_error", CaseID: msg.CaseID,
			Message: fmt.Sprintf("无法连接节点 %s：缺少 SSH 认证信息，请先建立 SSH 连接", msg.NodeIP),
		})
		return nil
	}

	connectMsg := auth
	connectMsg.Type = "ssh_connect"
	connectMsg.Host = msg.NodeIP
	connectMsg.NodeIP = msg.NodeIP

	log.Printf("[Bridge] autoConnect: 开始连接节点 %s (复用 case=%s 的认证 user=%s)",
		msg.NodeIP, msg.CaseID, auth.Username)
	session, err := newSSHSession(connectMsg)
	if err != nil {
		message, detail := buildSSHError(err)
		sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		log.Printf("[Bridge] autoConnect: 连接失败 node=%s case=%s err=%v", msg.NodeIP, msg.CaseID, err)
		return nil
	}

	stdout, err := session.start()
	if err != nil {
		session.close()
		message, detail := buildSSHError(err)
		sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		log.Printf("[Bridge] autoConnect: start 失败 node=%s case=%s err=%v", msg.NodeIP, msg.CaseID, err)
		return nil
	}

	key := sessionKey(msg.CaseID, msg.NodeIP)
	b.set(key, session)

	go session.on_output_start(ws, stdout, msg.CaseID, func() {
		b.remove(key)
	})

	log.Printf("[Bridge] autoConnect: 连接成功 node=%s key=%s", msg.NodeIP, key)
	return session
}

func sendMsg(ws *websocket.Conn, msg OutMessage) {
	data, _ := json.Marshal(msg)
	if err := websocket.Message.Send(ws, string(data)); err != nil {
		log.Printf("[Bridge] WebSocket 发送失败: type=%s case=%s err=%v", msg.Type, msg.CaseID, err)
	}
}

func (b *Bridge) handle(ws *websocket.Conn, customUI string) {
	cui := customUIHost(customUI)
	log.Printf("[Bridge] 浏览器已连接: origin=%s custom_ui=%s remote=%s", customUI, cui, ws.RemoteAddr())
	// 注册回采订阅者（按连接归属 custom_ui）
	sub := logHub.addSubscriber(ws, cui)
	// ownedSessions 追踪 ssh_connect 显式创建的会话
	ownedSessions := make(map[string]*SSHSession)
	defer func() {
		log.Printf("[Bridge] 浏览器已断开: custom_ui=%s remote=%s", cui, ws.RemoteAddr())
		logHub.removeSubscriber(ws)
		for key, owned := range ownedSessions {
			current := b.get(key)
			if current == owned {
				current.close()
				b.remove(key)
				log.Printf("[Bridge] 连接断开后清理会话: key=%s", key)
			}
		}
		// 清理该 caseID 的认证缓存
		// (保留，供下次连接使用)
	}()

	for {
		var raw string
		if err := websocket.Message.Receive(ws, &raw); err != nil {
			log.Printf("[Bridge] WebSocket 接收结束: remote=%v err=%v", ws.RemoteAddr(), err)
			break
		}

		var msg InMessage
		if err := json.Unmarshal([]byte(raw), &msg); err != nil {
			log.Printf("[Bridge] 消息解析失败: remote=%v err=%v raw=%q", ws.RemoteAddr(), err, raw)
			continue
		}
		log.Printf("[Bridge] 收到消息: type=%s case=%s node=%s container=%s",
			msg.Type, msg.CaseID, msg.NodeIP, msg.Container)

		switch msg.Type {

		case "ssh_connect":
			key := sessionKey(msg.CaseID, msg.NodeIP)
			if old := b.get(key); old != nil {
				old.close()
				b.remove(key)
				delete(ownedSessions, key)
			}
			// 如果没指定 nodeIP，也清理默认会话
			if msg.NodeIP == "" {
				defaultKey := sessionKey(msg.CaseID, "")
				if old := b.get(defaultKey); old != nil {
					old.close()
					b.remove(defaultKey)
					delete(ownedSessions, defaultKey)
				}
			}

			session, err := newSSHSession(msg)
			if err != nil {
				message, detail := buildSSHError(err)
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				continue
			}
			stdout, err := session.start()
			if err != nil {
				session.close()
				message, detail := buildSSHError(err)
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				continue
			}
			b.set(key, session)
			ownedSessions[key] = session
			// 保存认证信息，供后续自动连接其他节点使用
			b.lastAuth[msg.CaseID] = msg
			// 归属工单并回放近期日志（异常重传）
			logHub.setCase(sub, msg.CaseID)
			sendMsg(ws, OutMessage{Type: "ssh_connected", CaseID: msg.CaseID, CustomUI: cui})
			blog("INFO", "ssh.connected", "SSH 认证成功", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"user": msg.Username, "host": msg.Host, "port": msg.Port, "key": key,
			})
			log.Printf("[Bridge] SSH 认证成功: %s@%s:%d (key=%s)", msg.Username, msg.Host, msg.Port, key)

			caseID := msg.CaseID
			session.on_output_start(ws, stdout, caseID, func() {
				b.remove(key)
				delete(ownedSessions, key)
			})

		case "ssh_input":
			// 输入默认路由到 caseID 对应的会话
			defaultKey := sessionKey(msg.CaseID, "")
			if s := b.get(defaultKey); s != nil {
				s.send(msg.Data)
			}

		case "ssh_inject_command":
			defaultKey := sessionKey(msg.CaseID, "")
			if s := b.get(defaultKey); s != nil {
				s.injectCommand(msg.Command)
			}


			case "resume":
				// P0-2: 浏览器重连时触发历史日志回放
				if msg.CaseID != "" {
					logHub.setCase(sub, msg.CaseID)
					sendMsg(ws, OutMessage{Type: "resumed", CaseID: msg.CaseID, TraceID: msg.TraceID, CustomUI: cui})
					log.Printf("[Bridge] 收到 resume 信号: case=%s 触发日志回放", msg.CaseID)
				}

		case "ssh_disconnect":
			key := sessionKey(msg.CaseID, msg.NodeIP)
			if s := b.get(key); s != nil {
				s.close()
				b.remove(key)
				delete(ownedSessions, key)
			}
			// 同时清理默认会话
			if msg.NodeIP == "" || msg.NodeIP != "" {
				defaultKey := sessionKey(msg.CaseID, "")
				if s := b.get(defaultKey); s != nil && s != b.get(key) {
					s.close()
					b.remove(defaultKey)
					delete(ownedSessions, defaultKey)
				}
			}
			sendMsg(ws, OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})

		case "ssh_exec_command":
			// 根据 nodeIP 路由到正确的 SSH 会话
			s, key := b.resolveSession(msg)
			if s == nil && msg.NodeIP != "" {
				// 自动连接新节点
				s = b.autoConnectNode(ws, msg)
				key = sessionKey(msg.CaseID, msg.NodeIP)
			}
			// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话
			if s == nil && sub.caseID != "" {
				if fb := b.get(sessionKey(sub.caseID, "")); fb != nil {
					s = fb
					key = sessionKey(sub.caseID, "")
					log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", sub.caseID, key)
					// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
					blog("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, sub.caseID, msg.NodeIP, cui, map[string]any{
						"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
					})
				}
			}
			if s == nil {
				blog("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "key": sessionKey(msg.CaseID, msg.NodeIP),
					"sub_case_id": sub.caseID, "has_fallback_target": sub.caseID != "",
				})
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.ExecID == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "缺少 exec_id 参数", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.Command == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "缺少 command 参数", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}

			// 包装容器命令
			wrappedCmd := wrapContainerCommand(msg.Command, msg.Container)
			blog("INFO", "exec.start", "开始执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"exec_id": msg.ExecID, "key": key, "container": msg.Container, "cmd_len": len(wrappedCmd),
			})
			log.Printf("[Bridge] EXEC_START: key=%s node=%s container=%s exec_id=%s cmd_len=%d",
				key, msg.NodeIP, msg.Container, msg.ExecID, len(wrappedCmd))
			log.Printf("[Bridge] EXEC_CMD: %q", wrappedCmd)

			resultChan := s.execCommand(wrappedCmd, msg.ExecID, 60*time.Second)
			go func() {
				result := <-resultChan
				output := result.Output
				exitCode := result.ExitCode
				if result.Timeout {
					output = "execution timeout"
				}
				outLen := len(output)
				exitInfo := fmt.Sprintf("exit=%d", exitCode)
				if result.Timeout {
					exitInfo = "exit=TIMEOUT"
				}

					// P0-1: 增强日志完整性 - 回采命令执行的完整输出
					// 输出过长时截断为预览（避免日志字段过大），但完整记录长度和退出码
					outputPreview := output
					if len(outputPreview) > 1000 {
						outputPreview = outputPreview[:1000] + "...(截断)"
					}
					blog("INFO", "exec.output", "命令执行输出", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
						"exec_id":        msg.ExecID,
						"exit_code":      exitCode,
						"timeout":        result.Timeout,
						"output_len":     outLen,
						"output_preview": outputPreview,
						"command":        wrappedCmd,
					})

				blog("INFO", "exec.done", "命令执行完成", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "exit_code": exitCode, "timeout": result.Timeout, "output_len": outLen,
				})
				log.Printf("[Bridge] EXEC_DONE: exec_id=%s node=%s %s output_len=%d",
					msg.ExecID, msg.NodeIP, exitInfo, outLen)
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: output, ExitCode: exitCode,
					TraceID: msg.TraceID, CustomUI: cui,
				})
			}()

		case "ssh_exec_process":
			// 根据 nodeIP 路由到正确的 SSH 会话
			s, key := b.resolveSession(msg)
			if s == nil && msg.NodeIP != "" {
				s = b.autoConnectNode(ws, msg)
				key = sessionKey(msg.CaseID, msg.NodeIP)
			}
			// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话。
			// 因为一次 WebSocket 连接在 ssh_connect 后只存在一个会话，可据此兜底，
			// 避免空 case_id 导致 exec.session_missing、回采失败且无 DB 记录。
			if s == nil && sub.caseID != "" {
				if fb := b.get(sessionKey(sub.caseID, "")); fb != nil {
					s = fb
					key = sessionKey(sub.caseID, "")
					log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", sub.caseID, key)
					// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
					blog("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, sub.caseID, msg.NodeIP, cui, map[string]any{
						"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
					})
				}
			}
			if s == nil {
				blog("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令(隔离通道)", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "key": sessionKey(msg.CaseID, msg.NodeIP),
					"sub_case_id": sub.caseID, "has_fallback_target": sub.caseID != "",
				})
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.ExecID == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "缺少 exec_id 参数", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.Command == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "缺少 command 参数", ExitCode: -1,
				})
				continue
			}

// 包装容器命令
				wrappedCmd := wrapContainerCommand(msg.Command, msg.Container)
				log.Printf("[Bridge] EXEC_ISOLATED_START: key=%s node=%s container=%s exec_id=%s cmd_len=%d",
					key, msg.NodeIP, msg.Container, msg.ExecID, len(wrappedCmd))
				log.Printf("[Bridge] EXEC_ISOLATED_CMD: %q", wrappedCmd)

				// P0: 记录命令发起（包含 trace_id）
				blog("INFO", "exec.request", "收到命令执行请求", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id":   msg.ExecID,
					"command":   wrappedCmd,
					"container": msg.Container,
					"trace_id":  msg.TraceID,
				})

				go s.execCommandIsolated(ws, wrappedCmd, msg.ExecID)
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────

func (b *Bridge) corsWebSocketHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin == "" {
			origin = "*"
		}
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Access-Control-Allow-Private-Network", "true")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		if r.Method == "OPTIONS" {
			log.Printf("[Bridge] CORS 预检请求: origin=%s path=%s", origin, r.URL.Path)
			w.WriteHeader(http.StatusOK)
			return
		}
		log.Printf("[Bridge] WebSocket 请求: origin=%s method=%s", origin, r.Method)
		// 每个浏览器连接按 Origin 自动归属 custom_ui（本地 hci.local / 线上 acli.sangfor.com.cn:4443 等）
		wsHandler := websocket.Handler(func(ws *websocket.Conn) { b.handle(ws, origin) })
		wsHandler.ServeHTTP(w, r)
	})
}

// customUIHost 从完整 Origin 中提取可识别的标识（去 scheme），用于日志标签与回采关联。
func customUIHost(origin string) string {
	if origin == "" || origin == "*" {
		return "unknown"
	}
	o := origin
	if i := strings.Index(o, "://"); i >= 0 {
		o = o[i+3:]
	}
	return o
}

var (
	Version   = "v2.15.0-dev"
	CommitID  = "unknown" // 构建时通过 -X main.CommitID 注入 git commit
	BuildTime = "unknown" // 构建时通过 -X main.BuildTime 注入构建时间
)

func getVersion() string {
	if !strings.HasSuffix(Version, "-dev") {
		return Version
	}
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return Version
	}
	var revision string
	var modified bool
	for _, setting := range info.Settings {
		switch setting.Key {
		case "vcs.revision":
			revision = setting.Value
		case "vcs.modified":
			modified = setting.Value == "true"
		}
	}
	if revision == "" {
		return Version
	}
	if len(revision) > 8 {
		revision = revision[:8]
	}
	dirty := ""
	if modified {
		dirty = "-dirty"
	}
	return fmt.Sprintf("%s-g%s%s", Version, revision, dirty)
}


// P2-8: Prometheus 指标（简化实现 - 无外部依赖）
var (
	promMetrics = struct {
		LogsCollectedTotal   uint64
		LogsCollectErrors    uint64
		LogsReplayedTotal    uint64
		ExecCommandsTotal    uint64
		ExecCommandErrors    uint64
		SshConnectionsTotal  uint64
		SshConnectionErrors  uint64
	}{}
)

func getPrometheusMetrics() string {
	return fmt.Sprintf(`# HELP bridge_logs_collected_total Total logs collected
# TYPE bridge_logs_collected_total counter
bridge_logs_collected_total %d

# HELP bridge_logs_collect_errors_total Total log collection errors
# TYPE bridge_logs_collect_errors_total counter
bridge_logs_collect_errors_total %d

# HELP bridge_logs_replayed_total Total logs replayed on resume/restart
# TYPE bridge_logs_replayed_total counter
bridge_logs_replayed_total %d

# HELP bridge_exec_commands_total Total commands executed
# TYPE bridge_exec_commands_total counter
bridge_exec_commands_total %d

# HELP bridge_exec_command_errors_total Total command execution errors
# TYPE bridge_exec_command_errors_total counter
bridge_exec_command_errors_total %d

# HELP bridge_ssh_connections_total Total SSH connections
# TYPE bridge_ssh_connections_total counter
bridge_ssh_connections_total %d

# HELP bridge_ssh_connection_errors_total Total SSH connection errors
# TYPE bridge_ssh_connection_errors_total counter
bridge_ssh_connection_errors_total %d
`,
		promMetrics.LogsCollectedTotal,
		promMetrics.LogsCollectErrors,
		promMetrics.LogsReplayedTotal,
		promMetrics.ExecCommandsTotal,
		promMetrics.ExecCommandErrors,
		promMetrics.SshConnectionsTotal,
		promMetrics.SshConnectionErrors,
	)
}


// truncateString 截断字符串到指定长度
func truncateString(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}

func main() {
	showVersion := flag.Bool("version", false, "打印版本与 commit 信息后退出")
	flag.Parse()
	if *showVersion {
		fmt.Printf("terminal_bridge %s (commit: %s, built: %s)\n", getVersion(), CommitID, BuildTime)
		return
	}

	bridge := newBridge()
	// 把所有标准库日志重定向为结构化日志并回采（统一可观测性）
	log.SetOutput(bridgeLogWriter{})
	log.SetFlags(0)
	http.Handle("/", bridge.corsWebSocketHandler())
	
	// P2-8: Prometheus metrics 端点（供 Prometheus 抓取）
	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		w.Write([]byte(getPrometheusMetrics()))
	})

	addr := fmt.Sprintf("localhost:%d", wsPort)
	log.Printf("[Bridge] HCI SSH Bridge 已启动 (版本: %s, commit: %s, 构建时间: %s), 监听 ws://%s",
		getVersion(), CommitID, BuildTime, addr)
	log.Printf("[Bridge] CORS 已启用，支持从公网域名访问；日志结构化回采已开启")

	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatal("[Bridge] 启动失败:", err)
	}
}
