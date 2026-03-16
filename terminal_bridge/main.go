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

	"golang.org/x/crypto/ssh"
	"golang.org/x/net/websocket"
)

const (
	wsPort = 9999
)

var (
	// ANSI 控制序列清洗：CSI、OSC、以及单字符 ESC 序列。
	ansiCSI = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]`)
	ansiOSC = regexp.MustCompile(`\x1b\][^\x07\x1b]*(\x07|\x1b\\)`)
	ansiESC = regexp.MustCompile(`\x1b[@-Z\\-_]`)
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
}

type OutMessage struct {
	Type    string `json:"type"`
	CaseID  string `json:"case_id"`
	Output  string `json:"output,omitempty"`
	Message string `json:"message,omitempty"`
	Detail  string `json:"detail,omitempty"`
}

// ── SSH 会话 ──────────────────────────────────────────────────────────────────

type SSHSession struct {
	caseID  string
	client  *ssh.Client
	session *ssh.Session
	stdin   io.WriteCloser
	mu      sync.Mutex
	closed  bool
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
		caseID:  msg.CaseID,
		client:  client,
		session: session,
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

func sanitizeTerminalOutput(output string) string {
	if output == "" {
		return ""
	}

	cleaned := ansiOSC.ReplaceAllString(output, "")
	cleaned = ansiCSI.ReplaceAllString(cleaned, "")
	cleaned = ansiESC.ReplaceAllString(cleaned, "")

	var b strings.Builder
	b.Grow(len(cleaned))
	for _, r := range cleaned {
		// 保留常见可见字符和换行/回车/制表，过滤其余控制字符。
		if r == '\n' || r == '\r' || r == '\t' || r >= 0x20 {
			b.WriteRune(r)
		}
	}
	return b.String()
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
				log.Printf("[Bridge] 连接断开后清理会话: case=%s\\n", caseID)
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
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────

func main() {
	go runTray() // Windows 后台静默运行（tray_windows.go）

	bridge := newBridge()
	http.Handle("/", websocket.Handler(bridge.handle))

	addr := fmt.Sprintf("localhost:%d", wsPort)
	log.Printf("[Bridge] HCI SSH Bridge 已启动，监听 ws://%s\n", addr)

	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatal("[Bridge] 启动失败:", err)
	}
}
