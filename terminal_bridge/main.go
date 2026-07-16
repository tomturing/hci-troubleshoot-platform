// terminal_bridge - HCI 排障助手本地 SSH Bridge
// 架构: Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
// 编译: 执行 build_windows.bat 即可
// 体积: ~3-4MB 原生，upx 压缩后 ~1.5MB，支持 Win7/10/11，无任何运行时依赖

package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
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
	session, err := s.client.NewSession()
	if err != nil {
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("创建隔离 SSH 会话失败: %v", err), ExitCode: -1,
		})
		return
	}
	defer session.Close()

	stdoutPipe, err := session.StdoutPipe()
	if err != nil {
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("获取 StdoutPipe 失败: %v", err), ExitCode: -1,
		})
		return
	}
	stderrPipe, err := session.StderrPipe()
	if err != nil {
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: s.caseID, ExecID: execID,
			Stderr: fmt.Sprintf("获取 StderrPipe 失败: %v", err), ExitCode: -1,
		})
		return
	}

	if err := session.Start(command); err != nil {
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

func (b *Bridge) handle(ws *websocket.Conn) {
	log.Println("[Bridge] 浏览器已连接:", ws.RemoteAddr())
	// ownedSessions 追踪 ssh_connect 显式创建的会话
	ownedSessions := make(map[string]*SSHSession)
	defer func() {
		log.Println("[Bridge] 浏览器已断开:", ws.RemoteAddr())
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
			sendMsg(ws, OutMessage{Type: "ssh_connected", CaseID: msg.CaseID})
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
			if s == nil {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
				})
				continue
			}
			if msg.ExecID == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "缺少 exec_id 参数", ExitCode: -1,
				})
				continue
			}
			if msg.Command == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "缺少 command 参数", ExitCode: -1,
				})
				continue
			}

			// 包装容器命令
			wrappedCmd := wrapContainerCommand(msg.Command, msg.Container)
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
				log.Printf("[Bridge] EXEC_DONE: exec_id=%s node=%s %s output_len=%d",
					msg.ExecID, msg.NodeIP, exitInfo, outLen)
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: output, ExitCode: exitCode,
				})
			}()

		case "ssh_exec_process":
			// 根据 nodeIP 路由到正确的 SSH 会话
			s, key := b.resolveSession(msg)
			if s == nil && msg.NodeIP != "" {
				s = b.autoConnectNode(ws, msg)
				key = sessionKey(msg.CaseID, msg.NodeIP)
			}
			if s == nil {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
				})
				continue
			}
			if msg.ExecID == "" {
				sendMsg(ws, OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "缺少 exec_id 参数", ExitCode: -1,
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

			go s.execCommandIsolated(ws, wrappedCmd, msg.ExecID)
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────

func corsWebSocketHandler(wsHandler websocket.Handler) http.Handler {
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
		wsHandler.ServeHTTP(w, r)
	})
}

var (
	Version = "v2.15.0-dev"
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

func main() {
	bridge := newBridge()
	http.Handle("/", corsWebSocketHandler(websocket.Handler(bridge.handle)))

	addr := fmt.Sprintf("localhost:%d", wsPort)
	log.Printf("[Bridge] HCI SSH Bridge 已启动 (版本: %s), 监听 ws://%s", getVersion(), addr)
	log.Printf("[Bridge] CORS 已启用，支持从公网域名访问")

	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatal("[Bridge] 启动失败:", err)
	}
}
