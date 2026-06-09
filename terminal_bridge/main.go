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
	"strings"
	"sync"
	"time"

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
	ExecID     string `json:"exec_id"` // 用于 ssh_exec_command
}

type OutMessage struct {
	Type     string `json:"type"`
	CaseID   string `json:"case_id"`
	Output   string `json:"output,omitempty"`
	Message  string `json:"message,omitempty"`
	Detail   string `json:"detail,omitempty"`
	ExecID   string `json:"exec_id,omitempty"`   // 用于 exec_result
	ExitCode int    `json:"exit_code,omitempty"` // 用于 exec_result
}

// ── Exec Marker 监听器 ─────────────────────────────────────────────────────────

// ExecListener 用于追踪命令执行的 marker
type ExecListener struct {
	ExecID    string           // 执行 ID
	StartTime time.Time        // 开始时间
	OutputBuf *strings.Builder // 输出缓冲区
	ResultChan chan ExecResult // 结果通道
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

	port := msg.Port
	if port == 0 {
		port = 22
	}

	authMethods, err := buildAuthMethods(msg)
	if err != nil {
		return nil, err
	}

	clientConfig := &ssh.ClientConfig{
		User:            strings.TrimSpace(msg.Username),
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

// registerExecListener 注册一个 marker 监听器
func (s *SSHSession) registerExecListener(listener *ExecListener) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	s.listeners[listener.ExecID] = listener
	log.Printf("[Bridge] 注册 exec 监听器: case=%s exec_id=%s", s.caseID, listener.ExecID)
}

// unregisterExecListener 移除 marker 监听器
func (s *SSHSession) unregisterExecListener(execID string) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	delete(s.listeners, execID)
	log.Printf("[Bridge] 移除 exec 监听器: case=%s exec_id=%s", s.caseID, execID)
}

// appendOutput 将输出追加到所有活跃监听器的缓冲区
func (s *SSHSession) appendOutput(chunk string) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	for _, listener := range s.listeners {
		listener.OutputBuf.WriteString(chunk)
	}
}

// checkMarkers 检查输出中是否包含已注册的 marker
// 返回匹配到的监听器（如有）
func (s *SSHSession) checkMarkers(output string) (*ExecListener, int, bool) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()

	for execID, listener := range s.listeners {
		// marker 格式：__EXEC_DONE_{execId16}:{exit_code}
		// execId16 是 execID 的前 16 位
		normalizedExecID := strings.ReplaceAll(execID, "-", "")
		if len(normalizedExecID) < 16 {
			continue
		}
		markerPrefix := "__EXEC_DONE_" + normalizedExecID[:16] + ":"
		if idx := strings.Index(output, markerPrefix); idx != -1 {
			// 找到 marker，解析 exit_code
			markerStart := idx
			markerEnd := strings.IndexByte(output[markerStart:], '\n')
			if markerEnd == -1 {
				markerEnd = len(output) - markerStart
			}
			markerLine := output[markerStart : markerStart+markerEnd]

			// 解析 exit_code
			var exitCode int
			if _, err := fmt.Sscanf(markerLine, markerPrefix+"%d", &exitCode); err != nil {
				// 如果解析失败，判断是否为命令行回显（Echo）。回显行包含 "%s" 或 "$status" 特征，应忽略并继续等待真实输出
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

// execCommand 执行命令并注册监听器
func (s *SSHSession) execCommand(command, execID string, timeout time.Duration) <-chan ExecResult {
	resultChan := make(chan ExecResult, 1)

	// 创建监听器
	listener := &ExecListener{
		ExecID:     execID,
		StartTime:  time.Now(),
		OutputBuf:  &strings.Builder{},
		ResultChan: resultChan,
	}

	// 注册监听器
	s.registerExecListener(listener)

	// 启动超时检测
	go func() {
		select {
		case <-time.After(timeout):
			// 超时，发送超时结果
			s.listenersMu.Lock()
			if l, ok := s.listeners[execID]; ok {
				output := l.OutputBuf.String()
				delete(s.listeners, execID)
				s.listenersMu.Unlock()
				resultChan <- ExecResult{Output: output, ExitCode: -1, Timeout: true}
				log.Printf("[Bridge] exec 超时: case=%s exec_id=%s", s.caseID, execID)
			} else {
				s.listenersMu.Unlock()
			}
		case <-resultChan:
			// 正常完成
		}
	}()

	// 写入命令
	s.send(command)

	return resultChan
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
				// xterm.js 需要原始 ANSI/VT100 序列进行终端渲染，这里不再做清洗。
				chunk := string(buf[:n])

				// 追加到监听器缓冲区
				s.appendOutput(chunk)

				// 检查是否有 marker 匹配
				if listener, exitCode, matched := s.checkMarkers(chunk); matched {
					// 找到 marker，提取输出（不含 marker 行）
					output := listener.OutputBuf.String()
					// 移除 marker 行
					normalizedExecID := strings.ReplaceAll(listener.ExecID, "-", "")
					if len(normalizedExecID) >= 16 {
						markerPrefix := "__EXEC_DONE_" + normalizedExecID[:16] + ":"
						if idx := strings.Index(output, markerPrefix); idx != -1 {
							output = output[:idx]
							// 移除末尾可能的空行
							output = strings.TrimRight(output, "\r\n")
						}
					}

					// 发送 exec_result 消息
					sendMsg(ws, OutMessage{
						Type:     "exec_result",
						CaseID:   caseID,
						ExecID:   listener.ExecID,
						Output:   output,
						ExitCode: exitCode,
					})
					log.Printf("[Bridge] exec 完成: case=%s exec_id=%s exit_code=%d", caseID, listener.ExecID, exitCode)

					// 移除监听器并发送结果
					s.unregisterExecListener(listener.ExecID)
					select {
					case listener.ResultChan <- ExecResult{Output: output, ExitCode: exitCode}:
					default:
					}
				}

				// 发送原始输出到前端
				sendMsg(ws, OutMessage{
					Type:   "ssh_output",
					CaseID: caseID,
					Output: chunk,
				})
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
		log.Printf("[Bridge] SSH 会话已结束: case=%s", caseID)
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

// ── WebSocket Handler ─────────────────────────────────────────────────────────

type Bridge struct {
	mu       sync.Mutex
	sessions map[string]*SSHSession
}

func newBridge() *Bridge {
	return &Bridge{sessions: make(map[string]*SSHSession)}
}

func (b *Bridge) get(id string) *SSHSession {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.sessions[id]
}

func (b *Bridge) set(id string, s *SSHSession) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.sessions[id] = s
}

func (b *Bridge) remove(id string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.sessions, id)
}

func sendMsg(ws *websocket.Conn, msg OutMessage) {
	data, _ := json.Marshal(msg)
	if err := websocket.Message.Send(ws, string(data)); err != nil {
		log.Printf("[Bridge] WebSocket 发送失败: type=%s case=%s err=%v", msg.Type, msg.CaseID, err)
	}
}

func (b *Bridge) handle(ws *websocket.Conn) {
	log.Println("[Bridge] 浏览器已连接:", ws.RemoteAddr())
	ownedSessions := make(map[string]*SSHSession)
	defer func() {
		log.Println("[Bridge] 浏览器已断开:", ws.RemoteAddr())
		// 仅清理当前 WebSocket 连接创建的会话，避免误杀其他连接的 SSH 会话
		for caseID, owned := range ownedSessions {
			current := b.get(caseID)
			if current == owned {
				current.close()
				b.remove(caseID)
				log.Printf("[Bridge] 连接断开后清理会话: case=%s\n", caseID)
			}
		}
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
		log.Printf("[Bridge] 收到消息: type=%s case=%s remote=%v", msg.Type, msg.CaseID, ws.RemoteAddr())

		switch msg.Type {

		case "ssh_connect":
			if old := b.get(msg.CaseID); old != nil {
				old.close()
				b.remove(msg.CaseID)
				delete(ownedSessions, msg.CaseID)
			}
			session, err := newSSHSession(msg)
			if err != nil {
				message, detail := buildSSHError(err)
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				log.Printf("[Bridge] SSH 认证失败: case=%s message=%s detail=%s", msg.CaseID, message, detail)
				continue
			}
			stdout, err := session.start()
			if err != nil {
				session.close()
				message, detail := buildSSHError(err)
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				log.Printf("[Bridge] SSH 认证失败: case=%s message=%s detail=%s", msg.CaseID, message, detail)
				continue
			}
			b.set(msg.CaseID, session)
			ownedSessions[msg.CaseID] = session
			sendMsg(ws, OutMessage{Type: "ssh_connected", CaseID: msg.CaseID})
			log.Printf("[Bridge] SSH 认证成功: %s@%s:%d (case=%s)", msg.Username, msg.Host, msg.Port, msg.CaseID)

			// 异步读取 SSH 输出
			caseID := msg.CaseID
			session.on_output_start(
				ws,
				stdout,
				caseID,
				func() {
					b.remove(caseID)
					delete(ownedSessions, caseID)
				},
			)

		case "ssh_input":
			if s := b.get(msg.CaseID); s != nil {
				log.Printf("[Bridge] SSH 输入: case=%s bytes=%d", msg.CaseID, len(msg.Data))
				s.send(msg.Data)
			}

		case "ssh_inject_command":
			// AI 助手注入命令，不带 \n，等客户回车确认
			if s := b.get(msg.CaseID); s != nil {
				log.Printf("[Bridge] SSH 注入命令: case=%s bytes=%d", msg.CaseID, len(msg.Command))
				s.injectCommand(msg.Command)
			}

		case "ssh_disconnect":
			if s := b.get(msg.CaseID); s != nil {
				s.close()
				b.remove(msg.CaseID)
				delete(ownedSessions, msg.CaseID)
			}
			sendMsg(ws, OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})
			log.Printf("[Bridge] SSH 已断开: case=%s", msg.CaseID)

		case "ssh_exec_command":
			// T-TOOL-01: 执行命令并监听 marker
			s := b.get(msg.CaseID)
			if s == nil {
				sendMsg(ws, OutMessage{
					Type:     "exec_result",
					CaseID:   msg.CaseID,
					ExecID:   msg.ExecID,
					Output:   "SSH 会话不存在",
					ExitCode: -1,
				})
				log.Printf("[Bridge] ssh_exec_command 失败: 会话不存在 case=%s exec_id=%s", msg.CaseID, msg.ExecID)
				continue
			}

			if msg.ExecID == "" {
				sendMsg(ws, OutMessage{
					Type:     "exec_result",
					CaseID:   msg.CaseID,
					ExecID:   msg.ExecID,
					Output:   "缺少 exec_id 参数",
					ExitCode: -1,
				})
				log.Printf("[Bridge] ssh_exec_command 失败: 缺少 exec_id case=%s", msg.CaseID)
				continue
			}

			if msg.Command == "" {
				sendMsg(ws, OutMessage{
					Type:     "exec_result",
					CaseID:   msg.CaseID,
					ExecID:   msg.ExecID,
					Output:   "缺少 command 参数",
					ExitCode: -1,
				})
				log.Printf("[Bridge] ssh_exec_command 失败: 缺少 command case=%s exec_id=%s", msg.CaseID, msg.ExecID)
				continue
			}

			// 执行命令（60s 超时）
			log.Printf("[Bridge] 开始执行命令: case=%s exec_id=%s command_len=%d", msg.CaseID, msg.ExecID, len(msg.Command))
			resultChan := s.execCommand(msg.Command, msg.ExecID, 60*time.Second)

			// 异步等待结果并发送
			go func() {
				result := <-resultChan
				output := result.Output
				if result.Timeout {
					output = "execution timeout"
				}
				sendMsg(ws, OutMessage{
					Type:     "exec_result",
					CaseID:   msg.CaseID,
					ExecID:   msg.ExecID,
					Output:   output,
					ExitCode: result.ExitCode,
				})
			}()
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────

// corsWebSocketHandler 包装 websocket.Handler，添加 CORS 头支持
// 解决 Chrome Private Network Access (PNA) 限制：
// 从公网域名访问 localhost 时，浏览器要求服务端返回特定的 CORS 头
func corsWebSocketHandler(wsHandler websocket.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 获取请求来源，用于回填 Access-Control-Allow-Origin
		// PNA 预检要求返回具体的 origin，不能用 "*"
		origin := r.Header.Get("Origin")
		if origin == "" {
			origin = "*"
		}

		// 设置 CORS 头
		w.Header().Set("Access-Control-Allow-Origin", origin)
		// 关键：声明允许从公共网络访问私有网络
		w.Header().Set("Access-Control-Allow-Private-Network", "true")
		// 允许的请求方法和头
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")

		// 记录预检请求日志，便于调试
		if r.Method == "OPTIONS" {
			log.Printf("[Bridge] CORS 预检请求: origin=%s path=%s", origin, r.URL.Path)
			w.WriteHeader(http.StatusOK)
			return
		}

		// 非 OPTIONS 请求，交给 WebSocket handler 处理
		log.Printf("[Bridge] WebSocket 请求: origin=%s method=%s", origin, r.Method)
		wsHandler.ServeHTTP(w, r)
	})
}

func main() {
	bridge := newBridge()
	http.Handle("/", corsWebSocketHandler(websocket.Handler(bridge.handle)))

	addr := fmt.Sprintf("localhost:%d", wsPort)
	log.Printf("[Bridge] HCI SSH Bridge 已启动，监听 ws://%s", addr)
	log.Printf("[Bridge] CORS 已启用，支持从公网域名访问")

	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatal("[Bridge] 启动失败:", err)
	}
}