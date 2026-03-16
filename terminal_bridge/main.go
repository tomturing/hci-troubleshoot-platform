// terminal_bridge - HCI 排障助手本地 SSH Bridge
// 架构: Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
// 编译: 执行 build_windows.bat 即可
// 体积: ~3-4MB 原生，upx 压缩后 ~1.5MB，支持 Win7/10/11，无任何运行时依赖

package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/websocket"
)

const (
	wsPort = 9999
	sshBin = "ssh" // 使用 Windows 系统自带 ssh.exe
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
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	mu      sync.Mutex
	keyFile string
}

func newSSHSession(msg InMessage) (*SSHSession, error) {
	port := msg.Port
	if port == 0 {
		port = 22
	}

	args := []string{
		"-tt",
		"-o", "StrictHostKeyChecking=no",
		"-o", "BatchMode=no",
		"-p", fmt.Sprintf("%d", port),
	}

	var keyFile string
	if msg.AuthType == "key" && msg.PrivateKey != "" {
		f, err := os.CreateTemp("", "hci_bridge_*.pem")
		if err != nil {
			return nil, fmt.Errorf("创建临时私钥文件失败: %w", err)
		}
		if _, err := f.WriteString(msg.PrivateKey); err != nil {
			f.Close()
			os.Remove(f.Name())
			return nil, fmt.Errorf("写入私钥失败: %w", err)
		}
		f.Close()
		keyFile = f.Name()
		args = append(args, "-i", keyFile, "-o", "PasswordAuthentication=no")
	}

	args = append(args, fmt.Sprintf("%s@%s", msg.Username, msg.Host))

	cmd := exec.Command(sshBin, args...)
	setSysProcAttr(cmd) // 平台特定：Windows 隐藏窗口

	stdin, err := cmd.StdinPipe()
	if err != nil {
		if keyFile != "" {
			os.Remove(keyFile)
		}
		return nil, fmt.Errorf("获取 stdin 失败: %w", err)
	}

	return &SSHSession{
		caseID:  msg.CaseID,
		cmd:     cmd,
		stdin:   stdin,
		keyFile: keyFile,
	}, nil
}

func (s *SSHSession) start() (io.ReadCloser, error) {
	stdout, err := s.cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	s.cmd.Stderr = s.cmd.Stdout
	if err := s.cmd.Start(); err != nil {
		return nil, err
	}
	return stdout, nil
}

func (s *SSHSession) send(data string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stdin != nil {
		_, _ = io.WriteString(s.stdin, data)
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

func detectAuthSuccess(output string) bool {
	text := strings.ToLower(output)
	if strings.Contains(text, "last login") {
		return true
	}

	tail := output
	if len(tail) > 256 {
		tail = tail[len(tail)-256:]
	}
	trimmed := strings.TrimSpace(strings.ReplaceAll(tail, "\r", "\n"))
	if trimmed == "" {
		return false
	}

	// 常见 shell 提示符：$ / # / >
	return strings.HasSuffix(trimmed, "$") || strings.HasSuffix(trimmed, "#") || strings.HasSuffix(trimmed, ">")
}

func (s *SSHSession) on_output_start(
	ws *websocket.Conn,
	stdout io.ReadCloser,
	caseID string,
	onConnected func(),
	onAuthFailed func(message string, detail string),
	onExit func(),
) {
	go func() {
		buf := make([]byte, 4096)
		authBuffer := ""
		connectedSignaled := false
		failedSignaled := false

		for {
			n, err := stdout.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				sendMsg(ws, OutMessage{
					Type:   "ssh_output",
					CaseID: caseID,
					Output: chunk,
				})

				if !connectedSignaled && !failedSignaled {
					authBuffer += chunk
					if len(authBuffer) > 8192 {
						authBuffer = authBuffer[len(authBuffer)-8192:]
					}

					if message, detail, matched := classifySSHFailure(authBuffer); matched {
						failedSignaled = true
						onAuthFailed(message, detail)
						s.close()
						break
					}

					if detectAuthSuccess(authBuffer) {
						connectedSignaled = true
						onConnected()
					}
				}
			}
			if err != nil {
				break
			}
		}

		if !connectedSignaled && !failedSignaled {
			message, detail, matched := classifySSHFailure(authBuffer)
			if !matched {
				message = "SSH 会话提前结束，认证未成功"
			}
			onAuthFailed(message, detail)
		}
		sendMsg(ws, OutMessage{Type: "ssh_disconnected", CaseID: caseID})
		onExit()
		log.Printf("[Bridge] SSH 进程已退出: case=%s\n", caseID)
	}()
}

func (s *SSHSession) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stdin != nil {
		_ = s.stdin.Close()
		s.stdin = nil
	}
	if s.cmd != nil && s.cmd.Process != nil {
		_ = s.cmd.Process.Kill()
	}
	if s.keyFile != "" {
		os.Remove(s.keyFile)
		s.keyFile = ""
	}
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
	_ = websocket.Message.Send(ws, string(data))
}

func (b *Bridge) handle(ws *websocket.Conn) {
	log.Println("[Bridge] 浏览器已连接:", ws.RemoteAddr())
	defer func() {
		log.Println("[Bridge] 浏览器已断开:", ws.RemoteAddr())
		// 清理所有该连接的会话
		b.mu.Lock()
		for _, s := range b.sessions {
			s.close()
		}
		b.sessions = make(map[string]*SSHSession)
		b.mu.Unlock()
	}()

	for {
		var raw string
		if err := websocket.Message.Receive(ws, &raw); err != nil {
			break
		}

		var msg InMessage
		if err := json.Unmarshal([]byte(raw), &msg); err != nil {
			continue
		}

		switch msg.Type {

		case "ssh_connect":
			if old := b.get(msg.CaseID); old != nil {
				old.close()
				b.remove(msg.CaseID)
			}
			session, err := newSSHSession(msg)
			if err != nil {
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: err.Error()})
				continue
			}
			stdout, err := session.start()
			if err != nil {
				session.close()
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: err.Error()})
				continue
			}
			b.set(msg.CaseID, session)

			// 密码认证延迟发送
			if msg.AuthType == "password" && msg.Password != "" {
				pw := msg.Password
				go func() {
					time.Sleep(1500 * time.Millisecond)
					session.send(pw + "\n")
				}()
			}

			// 异步读取 SSH 输出
			caseID := msg.CaseID
			host := msg.Host
			port := msg.Port
			username := msg.Username
			session.on_output_start(
				ws,
				stdout,
				caseID,
				func() {
					sendMsg(ws, OutMessage{Type: "ssh_connected", CaseID: caseID})
					log.Printf("[Bridge] SSH 认证成功: %s@%s:%d (case=%s)\n", username, host, port, caseID)
				},
				func(message string, detail string) {
					sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: caseID, Message: message, Detail: detail})
					log.Printf("[Bridge] SSH 认证失败: %s@%s:%d (case=%s) message=%s detail=%s\n", username, host, port, caseID, message, detail)
				},
				func() {
					b.remove(caseID)
				},
			)

		case "ssh_input":
			if s := b.get(msg.CaseID); s != nil {
				s.send(msg.Data)
			}

		case "ssh_inject_command":
			// AI 助手注入命令，不带 \n，等客户回车确认
			if s := b.get(msg.CaseID); s != nil {
				s.injectCommand(msg.Command)
			}

		case "ssh_disconnect":
			if s := b.get(msg.CaseID); s != nil {
				s.close()
				b.remove(msg.CaseID)
			}
			sendMsg(ws, OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})
			log.Printf("[Bridge] SSH 已断开: case=%s\n", msg.CaseID)
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
