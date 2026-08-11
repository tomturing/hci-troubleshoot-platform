// terminal_bridge - HCI 排障助手 SSH Bridge
// 桌面架构: Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
// 集群架构: Custom UI (浏览器) → 同源 WebSocket → terminal_bridge Pod → SSH → HCI Linux
// 同一套代码同时构建 Windows 客户端和 Linux 容器镜像。

package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"runtime/debug"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
	"golang.org/x/net/websocket"
)

const (
	defaultWSPort = 9999
	desktopMode   = "desktop"
	clusterMode   = "cluster"
)

type runtimeConfig struct {
	Mode              string
	ListenAddress     string
	Port              int
	AllowedOrigins    []string
	AllowedOriginsRaw string
}

func envOrDefault(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envIntOrDefault(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func normalizeRuntimeConfig(mode, listenAddress string, port int, allowedOrigins string) (runtimeConfig, error) {
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = desktopMode
	}
	if mode != desktopMode && mode != clusterMode {
		return runtimeConfig{}, fmt.Errorf("不支持的运行模式 %q，仅支持 %s 或 %s", mode, desktopMode, clusterMode)
	}

	listenAddress = strings.TrimSpace(listenAddress)
	if listenAddress == "" {
		if mode == clusterMode {
			listenAddress = "0.0.0.0"
		} else {
			listenAddress = "127.0.0.1"
		}
	}
	if port < 1 || port > 65535 {
		return runtimeConfig{}, fmt.Errorf("监听端口必须在 1-65535 范围内，当前值为 %d", port)
	}

	allowedOrigins = strings.TrimSpace(allowedOrigins)
	if allowedOrigins == "" {
		if mode == clusterMode {
			allowedOrigins = "same-origin"
		} else {
			allowedOrigins = "*"
		}
	}
	origins := make([]string, 0, 4)
	for _, item := range strings.Split(allowedOrigins, ",") {
		item = strings.TrimRight(strings.TrimSpace(item), "/")
		if item != "" {
			origins = append(origins, item)
		}
	}
	if len(origins) == 0 {
		return runtimeConfig{}, fmt.Errorf("至少需要配置一个允许的 Origin")
	}

	return runtimeConfig{
		Mode:              mode,
		ListenAddress:     listenAddress,
		Port:              port,
		AllowedOrigins:    origins,
		AllowedOriginsRaw: allowedOrigins,
	}, nil
}

func (c runtimeConfig) address() string {
	return net.JoinHostPort(c.ListenAddress, strconv.Itoa(c.Port))
}

func (c runtimeConfig) originAllowed(origin, requestHost string) bool {
	if origin == "" {
		// 非浏览器客户端可能不发送 Origin；鉴权和访问控制由上层网络边界负责。
		return true
	}
	normalizedOrigin := strings.TrimRight(strings.TrimSpace(origin), "/")
	for _, allowed := range c.AllowedOrigins {
		switch allowed {
		case "*":
			return true
		case "same-origin":
			parsed, err := url.Parse(normalizedOrigin)
			if err == nil && strings.EqualFold(parsed.Host, requestHost) {
				return true
			}
		default:
			if strings.EqualFold(normalizedOrigin, allowed) {
				return true
			}
		}
	}
	return false
}

// ── 消息结构 ─────────────────────────────────────────────────────────────────

type InMessage struct {
	Type           string         `json:"type"`
	CaseID         string         `json:"case_id"`
	Host           string         `json:"host"`
	Username       string         `json:"username"`
	Port           int            `json:"port"`
	AuthType       string         `json:"auth_type"`
	Password       string         `json:"password"`
	PrivateKey     string         `json:"private_key"`
	Passphrase     string         `json:"passphrase"`
	Data           string         `json:"data"`
	Command        string         `json:"command"`
	ExecID         string         `json:"exec_id"`   // 用于 ssh_exec_command 和 ssh_exec_process
	NodeIP         string         `json:"node_ip"`   // 目标节点 IP（多节点路由）
	Container      string         `json:"container"` // 目标容器名（空或"host"=物理机直连）
	Timeout        int            `json:"timeout"`   // 命令最大执行秒数（1-300）
	TraceID        string         `json:"trace_id"`  // 端到端链路追踪 ID（Custom-UI → Bridge → Agent 统一）
	Traceparent    string         `json:"traceparent"`
	Tracestate     string         `json:"tracestate"`
	ExecutionMode  string         `json:"execution_mode,omitempty"`
	TestRunID      string         `json:"test_run_id,omitempty"`
	ConversationID string         `json:"conversation_id"`
	ToolCallID     string         `json:"tool_call_id"`
	Resume         bool           `json:"resume"`         // P0-2: 浏览器重连时发送 resume 信号，触发历史日志回放  // 端到端链路追踪 ID（Custom-UI → Bridge → Agent 统一）
	OutputFilters  []OutputFilter `json:"output_filters"` // 平台定义的安全逐行筛选，不执行 shell/正则
}

// OutputFilter 只能表达字面量行筛选，刻意不支持命令、正则、脚本和管道。
type OutputFilter struct {
	Source        string   `json:"source"`
	Include       []string `json:"include"`
	Exclude       []string `json:"exclude"`
	IncludeMode   string   `json:"include_mode"`
	ExcludeMode   string   `json:"exclude_mode,omitempty"`
	CaseSensitive bool     `json:"case_sensitive"`
}

type OutMessage struct {
	Type    string `json:"type"`
	CaseID  string `json:"case_id"`
	Output  string `json:"output,omitempty"`
	Message string `json:"message,omitempty"`
	Detail  string `json:"detail,omitempty"`
	ExecID  string `json:"exec_id,omitempty"` // 用于 exec_result
	// exec_result 必须始终携带 exit_code；0 是成功值，不能因 omitempty 被省略，
	// 否则浏览器会把缺失字段解析成 NaN，误判已成功的命令为失败。
	ExitCode        int    `json:"exit_code"`           // 用于 exec_result
	Stdout          string `json:"stdout,omitempty"`    // 双通道物理隔离输出 (Scheme B)
	Stderr          string `json:"stderr,omitempty"`    // 双通道物理隔离输出 (Scheme B)
	TraceID         string `json:"trace_id,omitempty"`  // 回显端到端 trace_id
	CustomUI        string `json:"custom_ui,omitempty"` // 来源 Custom-UI（自动按 Origin 关联）
	Traceparent     string `json:"traceparent,omitempty"`
	ArtifactID      string `json:"artifact_id,omitempty"`
	StdoutBytes     int64  `json:"stdout_bytes,omitempty"`
	StderrBytes     int64  `json:"stderr_bytes,omitempty"`
	StdoutSHA256    string `json:"stdout_sha256,omitempty"`
	StderrSHA256    string `json:"stderr_sha256,omitempty"`
	StdoutTruncated bool   `json:"stdout_truncated,omitempty"`
	StderrTruncated bool   `json:"stderr_truncated,omitempty"`
	DurationMS      int64  `json:"duration_ms,omitempty"`
	TimedOut        bool   `json:"timed_out,omitempty"`
	Cancelled       bool   `json:"cancelled,omitempty"`
	ErrorType       string `json:"error_type,omitempty"`
	// 模拟执行标记只来自已认证的 sim-ssh 租约上下文，绝不从 host 名推断。
	Simulation        bool   `json:"simulation,omitempty"`
	ExecutionMode     string `json:"execution_mode,omitempty"`
	SimulationBackend string `json:"simulation_backend,omitempty"`
}

type execRequestContext struct {
	Context         context.Context
	CaseID          string
	ConversationID  string
	ExecID          string
	ToolCallID      string
	TraceID         string
	Traceparent     string
	Tracestate      string
	TestRunID       string
	NodeIP          string
	Container       string
	CustomUI        string
	Command         string
	CommandRedacted string
	CommandSHA256   string
}

type boundedCapture struct {
	limit     int
	buffer    []byte
	total     int64
	hasher    hash.Hash
	truncated bool
}

func newBoundedCapture(limit int) *boundedCapture {
	return &boundedCapture{limit: limit, hasher: sha256.New()}
}

func (c *boundedCapture) write(p []byte) []byte {
	_, _ = c.hasher.Write(p)
	c.total += int64(len(p))
	remaining := c.limit - len(c.buffer)
	if remaining <= 0 {
		c.truncated = c.truncated || len(p) > 0
		return nil
	}
	if len(p) > remaining {
		c.truncated = true
		p = p[:remaining]
	}
	c.buffer = append(c.buffer, p...)
	return p
}

func (c *boundedCapture) String() string { return string(c.buffer) }
func (c *boundedCapture) SHA256() string { return fmt.Sprintf("%x", c.hasher.Sum(nil)) }

type streamStats struct {
	filtered     bool
	rawBytes     int64
	rawHasher    hash.Hash
	scannedLines int64
	keptLines    int64
	lastByte     byte
}

func newStreamStats(filtered bool) *streamStats {
	return &streamStats{filtered: filtered, rawHasher: sha256.New()}
}

func (s *streamStats) recordRaw(p []byte) {
	if len(p) == 0 {
		return
	}
	_, _ = s.rawHasher.Write(p)
	s.rawBytes += int64(len(p))
	s.scannedLines += int64(bytes.Count(p, []byte{'\n'}))
	s.lastByte = p[len(p)-1]
}

func (s *streamStats) finish() {
	if s.rawBytes > 0 && s.lastByte != '\n' {
		s.scannedLines++
	}
	if !s.filtered {
		s.keptLines = s.scannedLines
	}
}

func (s *streamStats) rawSHA256() string { return fmt.Sprintf("%x", s.rawHasher.Sum(nil)) }

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
	caseID            string
	client            *ssh.Client
	clientConfig      *ssh.ClientConfig
	address           string
	session           *ssh.Session
	stdin             io.WriteCloser
	mu                sync.Mutex
	closed            bool
	listenersMu       sync.Mutex
	listeners         map[string]*ExecListener // key: execID
	simulation        bool
	executionMode     string
	simulationBackend string
}

func isSimulationLeaseCredential(msg InMessage) bool {
	authType := strings.ToLower(strings.TrimSpace(msg.AuthType))
	return strings.EqualFold(strings.TrimSpace(msg.ExecutionMode), "sim-ssh") &&
		authType == "lease" && strings.HasPrefix(strings.TrimSpace(msg.Password), "htp2.")
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

	// 2. 真实 HCI 保持历史密码后缀行为；完整 htp2 Scenario Lease 必须原样传输。
	// lease 由 hci-sim 校验签名、时效、mode、bundle 和配额，Bridge 不解析载荷。
	authType := strings.TrimSpace(strings.ToLower(msg.AuthType))
	simulation := isSimulationLeaseCredential(msg)
	if (authType == "password" || authType == "") && msg.Password != "" && !simulation {
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

	addr := fmt.Sprintf("%s:%d", strings.TrimSpace(msg.Host), port)
	hostKeyCallback, err := buildHostKeyCallback(addr)
	if err != nil {
		return nil, fmt.Errorf("初始化 SSH 主机指纹校验失败: %w", err)
	}
	clientConfig := &ssh.ClientConfig{
		User:            username,
		Auth:            authMethods,
		HostKeyCallback: hostKeyCallback,
		Timeout:         12 * time.Second,
	}

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
		caseID:       msg.CaseID,
		client:       client,
		clientConfig: clientConfig,
		address:      addr,
		session:      session,
		listeners:    make(map[string]*ExecListener),
		simulation:   simulation,
		executionMode: func() string {
			if simulation {
				return "sim-ssh"
			}
			return ""
		}(),
		simulationBackend: func() string {
			if simulation {
				return "hci-sim"
			}
			return ""
		}(),
	}, nil
}

var knownHostsMu sync.Mutex

func buildHostKeyCallback(addr string) (ssh.HostKeyCallback, error) {
	policy := strings.ToLower(envOrDefault("HCI_BRIDGE_HOST_KEY_POLICY", "accept-new"))
	if policy == "insecure" {
		return ssh.InsecureIgnoreHostKey(), nil
	}
	knownHostsPath := strings.TrimSpace(os.Getenv("HCI_BRIDGE_KNOWN_HOSTS_FILE"))
	if knownHostsPath == "" {
		baseDir := envOrDefault("HCI_BRIDGE_LOG_DIR", ".")
		knownHostsPath = filepath.Join(baseDir, "known_hosts")
	}
	if err := os.MkdirAll(filepath.Dir(knownHostsPath), 0o700); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(knownHostsPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	_ = file.Close()
	strictCallback, err := knownhosts.New(knownHostsPath)
	if err != nil {
		return nil, err
	}
	return func(hostname string, remote net.Addr, key ssh.PublicKey) error {
		err := strictCallback(hostname, remote, key)
		if err == nil || policy == "strict" {
			return err
		}
		var keyErr *knownhosts.KeyError
		if !errors.As(err, &keyErr) || len(keyErr.Want) != 0 {
			return err
		}
		knownHostsMu.Lock()
		defer knownHostsMu.Unlock()
		line := knownhosts.Line([]string{knownhosts.Normalize(addr)}, key) + "\n"
		output, openErr := os.OpenFile(knownHostsPath, os.O_APPEND|os.O_WRONLY, 0o600)
		if openErr != nil {
			return openErr
		}
		_, writeErr := io.WriteString(output, line)
		closeErr := output.Close()
		if writeErr != nil {
			return writeErr
		}
		return closeErr
	}, nil
}

func buildAuthMethods(msg InMessage) ([]ssh.AuthMethod, error) {
	authType := strings.TrimSpace(strings.ToLower(msg.AuthType))
	methods := make([]ssh.AuthMethod, 0, 2)

	if authType == "password" || authType == "" || authType == "lease" {
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
		maxBytes := envIntOrDefault("HCI_BRIDGE_EXEC_MAX_OUTPUT_BYTES", 4*1024*1024)
		remaining := maxBytes - listener.OutputBuf.Len()
		if remaining > 0 {
			if len(chunk) > remaining {
				chunk = chunk[:remaining]
			}
			listener.OutputBuf.WriteString(chunk)
		}
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

// execCommandIsolated 独立建立 SSH 连接与 Session 执行命令（双通道事务执行设计）。
// 独立连接是硬超时的必要条件：仅关闭共享连接上的 Session 时，部分 SSH 服务端会等待
// 远端进程自然退出，导致 deadline 已触发但调用仍被阻塞；关闭独立连接才能确定性中止。
func (s *SSHSession) execCommandIsolated(ws *websocket.Conn, req execRequestContext, requestedTimeout time.Duration, outputFilters []OutputFilter) {
	// 模拟属性固定于连接创建时验证的认证上下文，host/DNS 名不会改变其语义。
	sim, execMode, simBackend := s.simulation, s.executionMode, s.simulationBackend

	timeout := requestedTimeout
	if timeout <= 0 {
		timeout = commandTimeout(envIntOrDefault("HCI_BRIDGE_EXEC_TIMEOUT_SECONDS", 120))
	}
	if err := validateOutputFilters(outputFilters); err != nil {
		errType := "invalid_output_filter"
		blogContext(req.Context, "ERROR", "exec.error", "输出筛选参数无效", req, map[string]any{
			"error": err.Error(), "error_type": errType,
		})
		sendMsg(ws, OutMessage{
			Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID,
			Stderr: err.Error(), ExitCode: -1, TraceID: req.TraceID,
			ErrorType: errType,
		})
		return
	}
	maxOutputBytes := envIntOrDefault("HCI_BRIDGE_EXEC_MAX_OUTPUT_BYTES", 4*1024*1024)
	ctx, cancel := context.WithTimeout(req.Context, timeout)
	defer cancel()
	ctx, span := otel.Tracer("terminal_bridge").Start(ctx, "terminal_bridge.ssh.exec",
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("exec.id", req.ExecID),
			attribute.String("case.id", req.CaseID),
			attribute.String("conversation.id", req.ConversationID),
			attribute.String("server.address", req.NodeIP),
			attribute.String("hci.container", req.Container),
			attribute.String("command.sha256", req.CommandSHA256),
		),
	)
	defer span.End()
	startTime := time.Now()
	traceparent := traceparentFromContext(ctx)
	traceID := span.SpanContext().TraceID().String()
	artifactID := deterministicArtifactID(req.ExecID)

	blogContext(ctx, "INFO", "exec.start", "开始执行命令", req, map[string]any{
		"command_redacted": req.CommandRedacted,
		"command_sha256":   req.CommandSHA256,
		"command_len":      len(req.Command),
	})

	isolatedClient, err := ssh.Dial("tcp", s.address, s.clientConfig)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "session_creation_failed")
		atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
		blogContext(ctx, "ERROR", "exec.error", "创建隔离 SSH 连接失败", req, map[string]any{"error": err.Error(), "error_type": "session_creation_failed"})
		sendMsg(ws, OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "创建隔离 SSH 连接失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "session_creation_failed"})
		return
	}
	defer isolatedClient.Close()

	session, err := isolatedClient.NewSession()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "session_creation_failed")
		atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
		blogContext(ctx, "ERROR", "exec.error", "创建隔离 SSH 会话失败", req, map[string]any{"error": err.Error(), "error_type": "session_creation_failed"})
		sendMsg(ws, OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "创建隔离 SSH 会话失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "session_creation_failed"})
		return
	}
	defer session.Close()
	if s.simulation {
		for name, value := range map[string]string{
			"TRACEPARENT":     traceparent,
			"TRACESTATE":      req.Tracestate,
			"HTP_EXEC_ID":     req.ExecID,
			"HTP_TEST_RUN_ID": req.TestRunID,
			"HTP_NODE_IP":     req.NodeIP,
			"HTP_CONTAINER":   req.Container,
		} {
			if strings.TrimSpace(value) == "" {
				continue
			}
			if err := session.Setenv(name, value); err != nil {
				span.RecordError(err)
				span.SetStatus(codes.Error, "simulation_env_rejected")
				atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
				blogContext(ctx, "ERROR", "exec.error", "hci-sim 拒绝执行上下文", req, map[string]any{"error": err.Error(), "error_type": "simulation_env_rejected", "env_name": name})
				sendMsg(ws, OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "hci-sim 拒绝执行上下文", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "simulation_env_rejected"})
				return
			}
		}
	}

	stdoutPipe, err := session.StdoutPipe()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "stdout_pipe_failed")
		atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
		blogContext(ctx, "ERROR", "exec.error", "获取 StdoutPipe 失败", req, map[string]any{"error": err.Error(), "error_type": "stdout_pipe_failed"})
		sendMsg(ws, OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "获取标准输出失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "stdout_pipe_failed"})
		return
	}
	stderrPipe, err := session.StderrPipe()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "stderr_pipe_failed")
		atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
		blogContext(ctx, "ERROR", "exec.error", "获取 StderrPipe 失败", req, map[string]any{"error": err.Error(), "error_type": "stderr_pipe_failed"})
		sendMsg(ws, OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "获取标准错误失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "stderr_pipe_failed"})
		return
	}

	if err := session.Start(req.Command); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "command_start_failed")
		atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
		blogContext(ctx, "ERROR", "exec.error", "启动命令失败", req, map[string]any{"error": err.Error(), "error_type": "command_start_failed"})
		sendMsg(ws, OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "启动命令失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "command_start_failed"})
		return
	}

	stdoutCapture := newBoundedCapture(maxOutputBytes)
	stderrCapture := newBoundedCapture(maxOutputBytes)
	stdoutFiltered := len(filtersForSource(outputFilters, "stdout")) > 0
	stderrFiltered := len(filtersForSource(outputFilters, "stderr")) > 0
	stdoutStats := newStreamStats(stdoutFiltered)
	stderrStats := newStreamStats(stderrFiltered)
	var readers sync.WaitGroup
	readers.Add(2)
	stdoutEmit := func(chunk string) {
		sendMsg(ws, OutMessage{Type: "exec_stdout", CaseID: req.CaseID, ExecID: req.ExecID, Stdout: chunk, TraceID: traceID})
	}
	stderrEmit := func(chunk string) {
		sendMsg(ws, OutMessage{Type: "exec_stderr", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: chunk, TraceID: traceID})
	}
	if stdoutFiltered {
		go drainExecPipeFiltered(stdoutPipe, stdoutCapture, stdoutStats, stdoutEmit, &readers, "stdout", outputFilters)
	} else {
		go drainExecPipe(stdoutPipe, stdoutCapture, stdoutStats, stdoutEmit, &readers)
	}
	if stderrFiltered {
		go drainExecPipeFiltered(stderrPipe, stderrCapture, stderrStats, stderrEmit, &readers, "stderr", outputFilters)
	} else {
		go drainExecPipe(stderrPipe, stderrCapture, stderrStats, stderrEmit, &readers)
	}

	waitCh := make(chan error, 1)
	go func() { waitCh <- session.Wait() }()
	timedOut := false
	waitErr := error(nil)
	select {
	case waitErr = <-waitCh:
	case <-ctx.Done():
		timedOut = errors.Is(ctx.Err(), context.DeadlineExceeded)
		_ = session.Close()
		_ = isolatedClient.Close()
		waitErr = <-waitCh
	}
	readers.Wait()
	stdoutStats.finish()
	stderrStats.finish()

	exitCode := 0
	errorType := ""
	if timedOut {
		exitCode = -1
		errorType = "timeout"
	} else if waitErr != nil {
		if exitErr, ok := waitErr.(*ssh.ExitError); ok {
			exitCode = exitErr.ExitStatus()
			errorType = "nonzero_exit"
		} else {
			exitCode = -1
			errorType = "ssh_wait_failed"
		}
	}
	if exitCode != 0 {
		atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
		span.SetStatus(codes.Error, errorType)
	}

	duration := time.Since(startTime)
	level := "INFO"
	if exitCode != 0 {
		level = "ERROR"
	}
	span.SetAttributes(
		attribute.Int("process.exit.code", exitCode),
		attribute.Int64("exec.duration_ms", duration.Milliseconds()),
		attribute.Int64("stdout.bytes", stdoutCapture.total),
		attribute.Int64("stderr.bytes", stderrCapture.total),
		attribute.Int64("stdout.raw_bytes", stdoutStats.rawBytes),
		attribute.Int64("stderr.raw_bytes", stderrStats.rawBytes),
		attribute.Int64("stdout.scanned_lines", stdoutStats.scannedLines),
		attribute.Int64("stderr.scanned_lines", stderrStats.scannedLines),
		attribute.Int64("stdout.kept_lines", stdoutStats.keptLines),
		attribute.Int64("stderr.kept_lines", stderrStats.keptLines),
		attribute.Bool("stdout.filter_applied", stdoutStats.filtered),
		attribute.Bool("stderr.filter_applied", stderrStats.filtered),
		attribute.Bool("stdout.truncated", stdoutCapture.truncated),
		attribute.Bool("stderr.truncated", stderrCapture.truncated),
		attribute.String("artifact.id", artifactID),
	)
	blogContext(ctx, level, "exec.done", "命令执行完成", req, map[string]any{
		"artifact_id":            artifactID,
		"exit_code":              exitCode,
		"success":                exitCode == 0,
		"error_type":             errorType,
		"duration_ms":            duration.Milliseconds(),
		"stdout_len":             stdoutCapture.total,
		"stderr_len":             stderrCapture.total,
		"stdout_sha256":          stdoutCapture.SHA256(),
		"stderr_sha256":          stderrCapture.SHA256(),
		"stdout_raw_bytes":       stdoutStats.rawBytes,
		"stderr_raw_bytes":       stderrStats.rawBytes,
		"stdout_raw_sha256":      stdoutStats.rawSHA256(),
		"stderr_raw_sha256":      stderrStats.rawSHA256(),
		"stdout_scanned_lines":   stdoutStats.scannedLines,
		"stderr_scanned_lines":   stderrStats.scannedLines,
		"stdout_kept_lines":      stdoutStats.keptLines,
		"stderr_kept_lines":      stderrStats.keptLines,
		"stdout_filter_applied":  stdoutStats.filtered,
		"stderr_filter_applied":  stderrStats.filtered,
		"stdout_filtered_bytes":  stdoutCapture.total,
		"stderr_filtered_bytes":  stderrCapture.total,
		"stdout_filtered_sha256": stdoutCapture.SHA256(),
		"stderr_filtered_sha256": stderrCapture.SHA256(),
		"stdout_truncated":       stdoutCapture.truncated,
		"stderr_truncated":       stderrCapture.truncated,
		"timed_out":              timedOut,
	})

	resultCtx, resultSpan := otel.Tracer("terminal_bridge").Start(ctx, "terminal_bridge.websocket.result.send",
		trace.WithAttributes(attribute.String("exec.id", req.ExecID)),
	)
	sendMsg(ws, OutMessage{
		Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID,
		Stdout: stdoutCapture.String(), Stderr: stderrCapture.String(), ExitCode: exitCode,
		TraceID: traceID, Traceparent: traceparentFromContext(resultCtx), ArtifactID: artifactID,
		StdoutBytes: stdoutCapture.total, StderrBytes: stderrCapture.total,
		StdoutSHA256: stdoutCapture.SHA256(), StderrSHA256: stderrCapture.SHA256(),
		StdoutTruncated: stdoutCapture.truncated, StderrTruncated: stderrCapture.truncated,
		DurationMS: duration.Milliseconds(), TimedOut: timedOut, ErrorType: errorType,
		Simulation: sim, ExecutionMode: execMode, SimulationBackend: simBackend,
	})
	resultSpan.End()
}

func drainExecPipe(pipe io.Reader, capture *boundedCapture, stats *streamStats, emit func(string), wg *sync.WaitGroup) {
	defer wg.Done()
	buffer := make([]byte, 4096)
	for {
		n, err := pipe.Read(buffer)
		if n > 0 {
			stats.recordRaw(buffer[:n])
			if captured := capture.write(buffer[:n]); len(captured) > 0 {
				emit(string(captured))
			}
		}
		if err != nil {
			return
		}
	}
}

func filtersForSource(filters []OutputFilter, source string) []OutputFilter {
	selected := make([]OutputFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Source == source && (len(filter.Include) > 0 || len(filter.Exclude) > 0) {
			selected = append(selected, filter)
		}
	}
	return selected
}

func lineMatchesOutputFilter(line string, filter OutputFilter) bool {
	candidate := line
	includes := filter.Include
	excludes := filter.Exclude
	if !filter.CaseSensitive {
		candidate = strings.ToLower(candidate)
		includes = make([]string, len(filter.Include))
		for i, value := range filter.Include {
			includes[i] = strings.ToLower(value)
		}
		excludes = make([]string, len(filter.Exclude))
		for i, value := range filter.Exclude {
			excludes[i] = strings.ToLower(value)
		}
	}
	includeOK := len(includes) == 0
	if len(includes) > 0 && filter.IncludeMode == "any" {
		for _, value := range includes {
			if strings.Contains(candidate, value) {
				includeOK = true
				break
			}
		}
	} else if len(includes) > 0 {
		includeOK = true
		for _, value := range includes {
			if !strings.Contains(candidate, value) {
				includeOK = false
				break
			}
		}
	}
	if !includeOK {
		return false
	}
	excludeMode := filter.ExcludeMode
	if excludeMode == "" {
		excludeMode = "any"
	}
	excludeHit := len(excludes) > 0 && excludeMode == "all"
	for _, value := range excludes {
		contains := strings.Contains(candidate, value)
		if excludeMode == "any" && contains {
			excludeHit = true
			break
		}
		if excludeMode == "all" && !contains {
			excludeHit = false
			break
		}
	}
	return !excludeHit
}

// drainExecPipeFiltered 在 Bridge 本地逐行筛选，原始大输出不会进入 WebSocket 和浏览器。
func drainExecPipeFiltered(pipe io.Reader, capture *boundedCapture, stats *streamStats, emit func(string), wg *sync.WaitGroup, source string, filters []OutputFilter) {
	defer wg.Done()
	selected := filtersForSource(filters, source)
	reader := bufio.NewReader(pipe)
	for {
		line, err := reader.ReadString('\n')
		if line != "" {
			stats.recordRaw([]byte(line))
			keep := false
			for _, filter := range selected {
				if lineMatchesOutputFilter(line, filter) {
					keep = true
					break
				}
			}
			if keep {
				stats.keptLines++
				if captured := capture.write([]byte(line)); len(captured) > 0 {
					emit(string(captured))
				}
			}
		}
		if err != nil {
			return
		}
	}
}

func validateOutputFilters(filters []OutputFilter) error {
	if len(filters) > 8 {
		return fmt.Errorf("output_filters 最多允许 8 项")
	}
	for index, filter := range filters {
		if filter.Source != "stdout" && filter.Source != "stderr" {
			return fmt.Errorf("output_filters[%d].source 只允许 stdout/stderr", index)
		}
		if filter.IncludeMode != "all" && filter.IncludeMode != "any" {
			return fmt.Errorf("output_filters[%d].include_mode 只允许 all/any", index)
		}
		if filter.ExcludeMode != "" && filter.ExcludeMode != "all" && filter.ExcludeMode != "any" {
			return fmt.Errorf("output_filters[%d].exclude_mode 只允许 all/any", index)
		}
		if len(filter.Include) > 8 || len(filter.Exclude) > 8 {
			return fmt.Errorf("output_filters[%d] 的 include/exclude 最多各 8 项", index)
		}
		if len(filter.Include) == 0 && len(filter.Exclude) == 0 {
			return fmt.Errorf("output_filters[%d] 至少需要 include 或 exclude", index)
		}
		for _, literal := range append(append([]string{}, filter.Include...), filter.Exclude...) {
			if len(literal) == 0 || len([]byte(literal)) > 512 {
				return fmt.Errorf("output_filters[%d] 条件必须为 1~512 字节的非空字面量", index)
			}
		}
	}
	return nil
}

func commandTimeout(seconds int) time.Duration {
	if seconds < 1 || seconds > 300 {
		seconds = 60
	}
	return time.Duration(seconds) * time.Second
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
	Type                  string         `json:"type"` // bridge_log - 前端监听 type==="bridge_log" 的必备字段
	Seq                   uint64         `json:"seq"`
	Timestamp             string         `json:"ts"`
	Level                 string         `json:"level"`
	Service               string         `json:"service"`
	Event                 string         `json:"event,omitempty"`
	Message               string         `json:"message,omitempty"`
	TraceID               string         `json:"trace_id,omitempty"`
	CaseID                string         `json:"case_id,omitempty"`
	NodeIP                string         `json:"node_ip,omitempty"`
	CustomUI              string         `json:"custom_ui,omitempty"`
	Extra                 map[string]any `json:"extra,omitempty"`
	Traceparent           string         `json:"traceparent,omitempty"` // P2-7: W3C traceparent 标准格式
	EventID               string         `json:"event_id"`
	BridgeInstanceID      string         `json:"bridge_instance_id"`
	SpanID                string         `json:"span_id,omitempty"`
	TraceFlags            string         `json:"trace_flags,omitempty"`
	ConversationID        string         `json:"conversation_id,omitempty"`
	ExecID                string         `json:"exec_id,omitempty"`
	ToolCallID            string         `json:"tool_call_id,omitempty"`
	ServiceName           string         `json:"service.name"`
	ServiceVersion        string         `json:"service.version"`
	DeploymentEnvironment string         `json:"deployment.environment"`
}

// bridgeSubscriber 代表一个已连接的 Custom-UI 浏览器（一个回采订阅者）。
type bridgeSubscriber struct {
	conn     *websocket.Conn
	customUI string
	caseID   string
	mu       sync.Mutex
	pending  []logEntry // 断网/重启期间的待重传缓冲（有界）
	flushing bool
}

// LogHub 全局日志中枢：结构化落盘 + 环形缓冲 + 多订阅者回采。
type LogHub struct {
	mu              sync.Mutex
	seq             uint64
	cap             int
	ring            []logEntry // 全局有界环形缓冲，供晚加入/重连回放
	subs            map[*websocket.Conn]*bridgeSubscriber
	logFile         *os.File
	logPath         string
	logFileBytes    int64
	maxLogFileBytes int64
	instanceID      string
}

var logHub = newLogHub()

func deterministicUUID(seed string) string {
	sum := sha256.Sum256([]byte(seed))
	sum[6] = (sum[6] & 0x0f) | 0x50
	sum[8] = (sum[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", sum[0:4], sum[4:6], sum[6:8], sum[8:10], sum[10:16])
}

func deterministicArtifactID(execID string) string { return deterministicUUID("artifact:" + execID) }
func deterministicEventID(instanceID string, seq uint64) string {
	return deterministicUUID(fmt.Sprintf("event:%s:%d", instanceID, seq))
}

func newBridgeInstanceID() string {
	hostname, _ := os.Hostname()
	return deterministicUUID(fmt.Sprintf("%s:%d:%d", hostname, os.Getpid(), time.Now().UnixNano()))
}

func newLogHub() *LogHub {
	h := &LogHub{
		cap:             5000,
		subs:            make(map[*websocket.Conn]*bridgeSubscriber),
		maxLogFileBytes: int64(envIntOrDefault("HCI_BRIDGE_LOG_MAX_BYTES", 64*1024*1024)),
		instanceID:      newBridgeInstanceID(),
	}
	// 本地持久化（重启回放）：best-effort，受 HCI_BRIDGE_LOG_DIR 控制。
	if dir := os.Getenv("HCI_BRIDGE_LOG_DIR"); dir != "" {
		_ = os.MkdirAll(dir, 0o755)
		logPath := filepath.Join(dir, "bridge.log")
		h.logPath = logPath
		if f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
			h.logFile = f
			if info, statErr := f.Stat(); statErr == nil {
				h.logFileBytes = info.Size()
			}
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
					if len(h.ring) >= h.cap {
						h.ring = h.ring[1:]
					}
					h.ring = append(h.ring, entry)
					if entry.Seq > h.seq {
						h.seq = entry.Seq
					}
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

func (h *LogHub) rotateLogFileLocked(nextEntryBytes int64) error {
	if h.logFile == nil || h.maxLogFileBytes <= 0 || h.logFileBytes+nextEntryBytes <= h.maxLogFileBytes {
		return nil
	}
	if err := h.logFile.Close(); err != nil {
		return err
	}
	h.logFile = nil
	rotatedPath := h.logPath + ".1"
	_ = os.Remove(rotatedPath)
	if err := os.Rename(h.logPath, rotatedPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	file, err := os.OpenFile(h.logPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	h.logFile = file
	h.logFileBytes = 0
	return nil
}

func (h *LogHub) publish(e logEntry) logEntry {
	h.mu.Lock()
	h.seq++
	e.Seq = h.seq
	e.Timestamp = time.Now().UTC().Format(time.RFC3339Nano)
	e.Type = "bridge_log" // 前端监听 type==="bridge_log" 的必备字段，统一在此注入
	e.BridgeInstanceID = h.instanceID
	e.EventID = deterministicEventID(h.instanceID, h.seq)
	e.ServiceName = "terminal-bridge"
	e.ServiceVersion = getVersion()
	e.DeploymentEnvironment = envOrDefault("HCI_DEPLOYMENT_ENVIRONMENT", "local")
	if len(h.ring) >= h.cap {
		h.ring = h.ring[1:]
	}
	h.ring = append(h.ring, e)
	if h.logFile != nil {
		if b, err := json.Marshal(e); err == nil {
			line := append(b, '\n')
			if rotateErr := h.rotateLogFileLocked(int64(len(line))); rotateErr != nil {
				atomic.AddUint64(&promMetrics.LogsCollectErrors, 1)
			} else if written, writeErr := h.logFile.Write(line); writeErr != nil {
				atomic.AddUint64(&promMetrics.LogsCollectErrors, 1)
			} else {
				h.logFileBytes += int64(written)
			}
		} else {
			atomic.AddUint64(&promMetrics.LogsCollectErrors, 1)
		}
	}
	eligibleSubscribers := make([]*bridgeSubscriber, 0, len(h.subs))
	for _, sub := range h.subs {
		if e.CaseID == "" || sub.caseID == "" || sub.caseID == e.CaseID {
			h.enqueue(sub, e)
			eligibleSubscribers = append(eligibleSubscribers, sub)
		}
	}
	h.mu.Unlock()

	// P2-8: 更新 Prometheus 指标
	atomic.AddUint64(&promMetrics.LogsCollectedTotal, 1)

	// 实时推送：publish 后立即异步 flush，保证 bridge_log 实时送达前端
	// （此前只在 setCase 时 flush，导致 ssh.connected 之后的日志全部滞留 pending queue）
	for _, sub := range eligibleSubscribers {
		go h.flushSubscriber(sub)
	}
	return e
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
	if sub.flushing {
		sub.mu.Unlock()
		return
	}
	sub.flushing = true
	for {
		if len(sub.pending) == 0 {
			sub.flushing = false
			sub.mu.Unlock()
			return
		}
		e := sub.pending[0]
		sub.pending = sub.pending[1:]
		sub.mu.Unlock()

		e.CustomUI = sub.customUI // 按订阅者归属 custom_ui，自动回采到对应后台
		b, err := json.Marshal(e)
		if err != nil {
			sub.mu.Lock()
			continue
		}
		if err := sendWebSocketRaw(sub.conn, string(b)); err != nil {
			sub.mu.Lock()
			if len(sub.pending) >= 2000 {
				sub.pending = sub.pending[:1999]
			}
			sub.pending = append([]logEntry{e}, sub.pending...)
			sub.flushing = false
			sub.mu.Unlock()
			return
		}
		sub.mu.Lock()
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

type logHubStatus struct {
	BufferedLogs int   `json:"buffered_logs"`
	Subscribers  int   `json:"subscribers"`
	PendingLogs  int   `json:"pending_logs"`
	LogFileBytes int64 `json:"log_file_bytes"`
}

func (h *LogHub) status() logHubStatus {
	h.mu.Lock()
	defer h.mu.Unlock()

	status := logHubStatus{
		BufferedLogs: len(h.ring),
		Subscribers:  len(h.subs),
		LogFileBytes: h.logFileBytes,
	}
	for _, sub := range h.subs {
		sub.mu.Lock()
		status.PendingLogs += len(sub.pending)
		sub.mu.Unlock()
	}
	return status
}

// bridgeLogWriter 把标准库 log 输出重定向为结构化日志并回采，从而零改造捕获全部既有 log.Printf。
type bridgeLogWriter struct{}

func (bridgeLogWriter) Write(p []byte) (int, error) {
	line := redactSensitiveText(strings.TrimRight(string(p), "\n"))
	level := "INFO"
	upper := strings.ToUpper(line)
	switch {
	case strings.Contains(upper, "ERROR"):
		level = "ERROR"
	case strings.Contains(upper, "WARNING"), strings.Contains(upper, "WARN"):
		level = "WARN"
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
	e := logHub.publish(logEntry{Level: level, Service: "terminal_bridge", Event: event, Message: msg})
	b, err := json.Marshal(e)
	if err != nil {
		return len(p), nil
	}
	_, err = os.Stdout.Write(append(b, '\n'))
	return len(p), err
}

// blog 记录带上下文（trace/case/node/custom_ui）的结构化日志并回采。
func blog(level, event, msg, traceID, caseID, nodeIP, customUI string, extra map[string]any) {
	e := logEntry{
		Level:    normalizeLogLevel(level),
		Service:  "terminal_bridge",
		Event:    event,
		Message:  redactSensitiveText(msg),
		TraceID:  traceID,
		CaseID:   caseID,
		NodeIP:   nodeIP,
		CustomUI: customUI,
		Extra:    sanitizeExtra(extra),
	}
	e = logHub.publish(e)
	if b, err := json.Marshal(e); err == nil {
		os.Stdout.Write(append(b, '\n'))
	}
}

func blogContext(ctx context.Context, level, event, message string, req execRequestContext, extra map[string]any) {
	spanContext := trace.SpanContextFromContext(ctx)
	e := logEntry{
		Level: normalizeLogLevel(level), Service: "terminal_bridge", Event: event,
		Message: message, TraceID: spanContext.TraceID().String(), SpanID: spanContext.SpanID().String(),
		TraceFlags: fmt.Sprintf("%02x", byte(spanContext.TraceFlags())), Traceparent: traceparentFromContext(ctx),
		CaseID: req.CaseID, NodeIP: req.NodeIP, CustomUI: req.CustomUI,
		ConversationID: req.ConversationID, ExecID: req.ExecID, ToolCallID: req.ToolCallID,
		Extra: sanitizeExtra(extra),
	}
	e = logHub.publish(e)
	if encoded, err := json.Marshal(e); err == nil {
		_, _ = os.Stdout.Write(append(encoded, '\n'))
	}
}

func normalizeLogLevel(level string) string {
	level = strings.ToUpper(strings.TrimSpace(level))
	if level == "WARNING" {
		return "WARN"
	}
	return level
}

var sensitiveTextPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*|\s+)([^\s'\"]+|['\"][^'\"]*['\"])`),
	regexp.MustCompile(`(?s)"(private_key|passphrase|password)"\s*:\s*"[^"]*"`),
}

func redactSensitiveText(value string) string {
	redacted := value
	for _, pattern := range sensitiveTextPatterns {
		redacted = pattern.ReplaceAllString(redacted, `$1$2[REDACTED]`)
	}
	return redacted
}

func sanitizeExtra(extra map[string]any) map[string]any {
	if extra == nil {
		return nil
	}
	sanitized := make(map[string]any, len(extra))
	for key, value := range extra {
		lowerKey := strings.ToLower(key)
		if strings.Contains(lowerKey, "password") || strings.Contains(lowerKey, "secret") || strings.Contains(lowerKey, "private_key") || strings.Contains(lowerKey, "passphrase") || strings.Contains(lowerKey, "token") {
			sanitized[key] = "[REDACTED]"
			continue
		}
		if textValue, ok := value.(string); ok {
			sanitized[key] = redactSensitiveText(textValue)
		} else {
			sanitized[key] = value
		}
	}
	return sanitized
}

func redactCommand(command string) string {
	redacted := redactSensitiveText(command)
	if len(redacted) > 2048 {
		return redacted[:2048] + "...(截断)"
	}
	return redacted
}

func commandSHA256(command string) string {
	sum := sha256.Sum256([]byte(command))
	return fmt.Sprintf("%x", sum[:])
}

func traceparentFromContext(ctx context.Context) string {
	spanContext := trace.SpanContextFromContext(ctx)
	if !spanContext.IsValid() {
		return ""
	}
	return fmt.Sprintf("00-%s-%s-%02x", spanContext.TraceID(), spanContext.SpanID(), byte(spanContext.TraceFlags()))
}

func normalizeTraceparentForLegacyGo(traceparent string) string {
	// Python OTel 会按新版 W3C Trace Context 生成 Random 标志位（0x02），因此已采样链路可能携带 0x03。
	// 当前 Go OTel v1.24 的 version 00 解析器只接受 0x00/0x01，会把合法的 0x02/0x03 静默当成无效父上下文。
	// Go SDK 尚不能表达 Random 标志位，这里只丢弃 Random 位并保留 Sampled 位；其他保留位继续交给官方解析器拒绝。
	parts := strings.Split(traceparent, "-")
	if len(parts) != 4 || parts[0] != "00" {
		return traceparent
	}
	switch parts[3] {
	case "02":
		parts[3] = "00"
	case "03":
		parts[3] = "01"
	default:
		return traceparent
	}
	return strings.Join(parts, "-")
}

func contextFromMessage(msg InMessage) context.Context {
	carrier := propagation.MapCarrier{}
	if msg.Traceparent != "" {
		carrier.Set("traceparent", normalizeTraceparentForLegacyGo(msg.Traceparent))
	}
	if msg.Tracestate != "" {
		carrier.Set("tracestate", msg.Tracestate)
	}
	return otel.GetTextMapPropagator().Extract(context.Background(), carrier)
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

func (b *Bridge) setLastAuth(caseID string, msg InMessage) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.lastAuth[caseID] = msg
}

func (b *Bridge) getLastAuth(caseID string) (InMessage, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	msg, ok := b.lastAuth[caseID]
	return msg, ok
}

func (b *Bridge) clearLastAuth(caseID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.lastAuth, caseID)
}

type bridgeStatus struct {
	ActiveSessions  int `json:"active_sessions"`
	CachedAuthCases int `json:"cached_auth_cases"`
}

func (b *Bridge) status() bridgeStatus {
	b.mu.Lock()
	defer b.mu.Unlock()
	return bridgeStatus{
		ActiveSessions:  len(b.sessions),
		CachedAuthCases: len(b.lastAuth),
	}
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
func (b *Bridge) autoConnectNode(ws *websocket.Conn, msg InMessage, ownedSessions *ownedSessionTracker) *SSHSession {
	if msg.NodeIP == "" {
		log.Printf("[Bridge] autoConnect: nodeIP 为空，跳过")
		return nil
	}

	auth, ok := b.getLastAuth(msg.CaseID)
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
		atomic.AddUint64(&promMetrics.SshConnectionErrors, 1)
		message, detail := buildSSHError(err)
		sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		log.Printf("[Bridge] autoConnect: 连接失败 node=%s case=%s err=%v", msg.NodeIP, msg.CaseID, err)
		return nil
	}

	stdout, err := session.start()
	if err != nil {
		atomic.AddUint64(&promMetrics.SshConnectionErrors, 1)
		session.close()
		message, detail := buildSSHError(err)
		sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		log.Printf("[Bridge] autoConnect: start 失败 node=%s case=%s err=%v", msg.NodeIP, msg.CaseID, err)
		return nil
	}

	key := sessionKey(msg.CaseID, msg.NodeIP)
	b.set(key, session)
	ownedSessions.set(key, session)
	atomic.AddUint64(&promMetrics.SshConnectionsTotal, 1)

	go session.on_output_start(ws, stdout, msg.CaseID, func() {
		b.remove(key)
		ownedSessions.remove(key)
	})

	log.Printf("[Bridge] autoConnect: 连接成功 node=%s key=%s", msg.NodeIP, key)
	return session
}

var websocketWriteLocks sync.Map

func sendWebSocketRaw(ws *websocket.Conn, payload string) error {
	lockValue, _ := websocketWriteLocks.LoadOrStore(ws, &sync.Mutex{})
	lock := lockValue.(*sync.Mutex)
	lock.Lock()
	defer lock.Unlock()
	return websocket.Message.Send(ws, payload)
}

func sendMsg(ws *websocket.Conn, msg OutMessage) {
	data, _ := json.Marshal(msg)
	if err := sendWebSocketRaw(ws, string(data)); err != nil {
		log.Printf("[Bridge] WebSocket 发送失败: type=%s case=%s err=%v", msg.Type, msg.CaseID, err)
	}
}

type ownedSessionTracker struct {
	mu       sync.Mutex
	sessions map[string]*SSHSession
}

func newOwnedSessionTracker() *ownedSessionTracker {
	return &ownedSessionTracker{sessions: make(map[string]*SSHSession)}
}

func (t *ownedSessionTracker) set(key string, session *SSHSession) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.sessions[key] = session
}

func (t *ownedSessionTracker) remove(key string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.sessions, key)
}

// drainCase 取出当前 WebSocket 所拥有的指定工单全部会话，包括自动建立的节点会话。
func (t *ownedSessionTracker) drainCase(caseID string) map[string]*SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	sessions := make(map[string]*SSHSession)
	for key, session := range t.sessions {
		if key == caseID || strings.HasPrefix(key, caseID+"@") {
			sessions[key] = session
			delete(t.sessions, key)
		}
	}
	return sessions
}

func (t *ownedSessionTracker) drain() map[string]*SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	sessions := t.sessions
	t.sessions = make(map[string]*SSHSession)
	return sessions
}

func (b *Bridge) handle(ws *websocket.Conn, customUI string) {
	cui := customUIHost(customUI)
	log.Printf("[Bridge] 浏览器已连接: origin=%s custom_ui=%s remote=%s", customUI, cui, ws.RemoteAddr())
	// 注册回采订阅者（按连接归属 custom_ui）
	sub := logHub.addSubscriber(ws, cui)
	// ownedSessions 追踪 ssh_connect 显式创建的会话
	ownedSessions := newOwnedSessionTracker()
	defer func() {
		log.Printf("[Bridge] 浏览器已断开: custom_ui=%s remote=%s", cui, ws.RemoteAddr())
		logHub.removeSubscriber(ws)
		websocketWriteLocks.Delete(ws)
		for key, owned := range ownedSessions.drain() {
			current := b.get(key)
			if current == owned {
				current.close()
				b.remove(key)
				log.Printf("[Bridge] 连接断开后清理会话: key=%s", key)
			}
		}
		// 浏览器断开后立即清理凭据缓存，缩短密码/私钥在内存中的生命周期。
		b.clearLastAuth(sub.caseID)
	}()

	for {
		var raw string
		if err := websocket.Message.Receive(ws, &raw); err != nil {
			log.Printf("[Bridge] WebSocket 接收结束: remote=%v err=%v", ws.RemoteAddr(), err)
			break
		}

		var msg InMessage
		if err := json.Unmarshal([]byte(raw), &msg); err != nil {
			log.Printf("[Bridge] 消息解析失败: remote=%v err=%v payload_bytes=%d", ws.RemoteAddr(), err, len(raw))
			continue
		}
		log.Printf("[Bridge] 收到消息: type=%s case=%s node=%s container=%s",
			msg.Type, msg.CaseID, msg.NodeIP, msg.Container)

		switch msg.Type {

		case "ssh_connect":
			connectCtx, connectSpan := otel.Tracer("terminal_bridge").Start(
				contextFromMessage(msg), "terminal_bridge.ssh.connect",
				trace.WithSpanKind(trace.SpanKindClient),
				trace.WithAttributes(attribute.String("case.id", msg.CaseID), attribute.String("server.address", msg.Host)),
			)
			_ = connectCtx
			key := sessionKey(msg.CaseID, msg.NodeIP)
			if old := b.get(key); old != nil {
				old.close()
				b.remove(key)
				ownedSessions.remove(key)
			}
			// 如果没指定 nodeIP，也清理默认会话
			if msg.NodeIP == "" {
				defaultKey := sessionKey(msg.CaseID, "")
				if old := b.get(defaultKey); old != nil {
					old.close()
					b.remove(defaultKey)
					ownedSessions.remove(defaultKey)
				}
			}

			session, err := newSSHSession(msg)
			if err != nil {
				connectSpan.RecordError(err)
				connectSpan.SetStatus(codes.Error, "ssh_connect_failed")
				connectSpan.End()
				atomic.AddUint64(&promMetrics.SshConnectionErrors, 1)
				message, detail := buildSSHError(err)
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				continue
			}
			stdout, err := session.start()
			if err != nil {
				connectSpan.RecordError(err)
				connectSpan.SetStatus(codes.Error, "ssh_start_failed")
				connectSpan.End()
				atomic.AddUint64(&promMetrics.SshConnectionErrors, 1)
				session.close()
				message, detail := buildSSHError(err)
				sendMsg(ws, OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				continue
			}
			b.set(key, session)
			connectSpan.End()
			atomic.AddUint64(&promMetrics.SshConnectionsTotal, 1)
			ownedSessions.set(key, session)
			// 保存认证信息，供后续自动连接其他节点使用
			b.setLastAuth(msg.CaseID, msg)
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
				ownedSessions.remove(key)
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
			// 关闭当前 WebSocket 所拥有的该工单全部会话，防止自动节点会话泄漏。
			for key, owned := range ownedSessions.drainCase(msg.CaseID) {
				if current := b.get(key); current == owned {
					current.close()
					b.remove(key)
				}
			}
			sendMsg(ws, OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})

		case "ssh_exec_command":
			// 根据 nodeIP 路由到正确的 SSH 会话
			s, key := b.resolveSession(msg)
			if s == nil && msg.NodeIP != "" {
				// 自动连接新节点
				s = b.autoConnectNode(ws, msg, ownedSessions)
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
			log.Printf("[Bridge] EXEC_CMD: sha256=%s redacted=%q", commandSHA256(wrappedCmd), redactCommand(wrappedCmd))

			resultChan := s.execCommand(wrappedCmd, msg.ExecID, 60*time.Second)
			atomic.AddUint64(&promMetrics.ExecCommandsTotal, 1)
			go func() {
				result := <-resultChan
				output := result.Output
				exitCode := result.ExitCode
				if result.Timeout {
					output = "execution timeout"
				}
				if result.Timeout || exitCode != 0 {
					atomic.AddUint64(&promMetrics.ExecCommandErrors, 1)
				}
				outLen := len(output)
				exitInfo := fmt.Sprintf("exit=%d", exitCode)
				if result.Timeout {
					exitInfo = "exit=TIMEOUT"
				}

				// P0-1: 增强日志完整性 - 回采命令执行的完整输出
				// 输出过长时截断为预览（避免日志字段过大），但完整记录长度和退出码
				blog("INFO", "exec.output", "命令执行输出", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id":          msg.ExecID,
					"exit_code":        exitCode,
					"timeout":          result.Timeout,
					"output_len":       outLen,
					"output_sha256":    commandSHA256(output),
					"command_redacted": redactCommand(wrappedCmd),
					"command_sha256":   commandSHA256(wrappedCmd),
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
				s = b.autoConnectNode(ws, msg, ownedSessions)
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
			commandHash := commandSHA256(wrappedCmd)

			// P0: 记录命令发起（包含 trace_id）
			blog("INFO", "exec.request", "收到命令执行请求", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"exec_id":          msg.ExecID,
				"command_redacted": redactCommand(wrappedCmd),
				"command_sha256":   commandHash,
				"container":        msg.Container,
				"trace_id":         msg.TraceID,
			})

			atomic.AddUint64(&promMetrics.ExecCommandsTotal, 1)
			receiveCtx, receiveSpan := otel.Tracer("terminal_bridge").Start(
				contextFromMessage(msg), "terminal_bridge.websocket.receive",
				trace.WithAttributes(attribute.String("exec.id", msg.ExecID), attribute.String("message.type", msg.Type)),
			)
			receiveSpan.End()
			req := execRequestContext{
				Context: receiveCtx, CaseID: msg.CaseID, ConversationID: msg.ConversationID,
				ExecID: msg.ExecID, ToolCallID: msg.ToolCallID, TraceID: msg.TraceID,
				Traceparent: msg.Traceparent, Tracestate: msg.Tracestate, TestRunID: msg.TestRunID, NodeIP: msg.NodeIP,
				Container: msg.Container, CustomUI: cui, Command: wrappedCmd,
				CommandRedacted: redactCommand(wrappedCmd), CommandSHA256: commandHash,
			}
			var requestedTimeout time.Duration
			if msg.Timeout > 0 {
				requestedTimeout = commandTimeout(msg.Timeout)
			}
			go s.execCommandIsolated(ws, req, requestedTimeout, msg.OutputFilters)
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────

func (b *Bridge) corsWebSocketHandler(config runtimeConfig) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if !config.originAllowed(origin, r.Host) {
			blog("WARNING", "websocket.origin_rejected", "拒绝非授权 Origin 的 WebSocket 请求", "", "", "", customUIHost(origin), map[string]any{
				"origin": origin,
				"host":   r.Host,
				"mode":   config.Mode,
			})
			http.Error(w, "origin not allowed", http.StatusForbidden)
			return
		}
		responseOrigin := origin
		if responseOrigin == "" {
			responseOrigin = "*"
		}
		w.Header().Set("Access-Control-Allow-Origin", responseOrigin)
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
		wsHandler := websocket.Handler(func(ws *websocket.Conn) {
			atomic.AddInt64(&activeWebSockets, 1)
			defer atomic.AddInt64(&activeWebSockets, -1)
			b.handle(ws, origin)
		})
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
	Version   = "v2.16.0-dev"
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
		LogsCollectedTotal  uint64
		LogsCollectErrors   uint64
		LogsReplayedTotal   uint64
		ExecCommandsTotal   uint64
		ExecCommandErrors   uint64
		SshConnectionsTotal uint64
		SshConnectionErrors uint64
	}{}
	activeWebSockets int64
)

func prometheusLabelValue(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	value = strings.ReplaceAll(value, `"`, `\"`)
	return strings.ReplaceAll(value, "\n", `\n`)
}

func getPrometheusMetrics(bridge *Bridge, config runtimeConfig) string {
	bridgeState := bridge.status()
	logState := logHub.status()
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

# HELP bridge_process_up Whether the terminal_bridge process is serving requests
# TYPE bridge_process_up gauge
bridge_process_up 1

# HELP bridge_build_info Build and runtime information for terminal_bridge
# TYPE bridge_build_info gauge
bridge_build_info{version="%s",commit="%s",mode="%s"} 1

# HELP bridge_websocket_connections_active Current browser WebSocket connections
# TYPE bridge_websocket_connections_active gauge
bridge_websocket_connections_active %d

# HELP bridge_ssh_sessions_active Current active SSH sessions
# TYPE bridge_ssh_sessions_active gauge
bridge_ssh_sessions_active %d

# HELP bridge_log_subscribers_active Current log collection subscribers
# TYPE bridge_log_subscribers_active gauge
bridge_log_subscribers_active %d

# HELP bridge_log_buffer_entries Current entries in the in-memory log replay buffer
# TYPE bridge_log_buffer_entries gauge
bridge_log_buffer_entries %d

# HELP bridge_log_pending_entries Current log entries waiting for collection
# TYPE bridge_log_pending_entries gauge
bridge_log_pending_entries %d

# HELP bridge_log_file_bytes Current persisted bridge log file size in bytes
# TYPE bridge_log_file_bytes gauge
bridge_log_file_bytes %d
`,
		atomic.LoadUint64(&promMetrics.LogsCollectedTotal),
		atomic.LoadUint64(&promMetrics.LogsCollectErrors),
		atomic.LoadUint64(&promMetrics.LogsReplayedTotal),
		atomic.LoadUint64(&promMetrics.ExecCommandsTotal),
		atomic.LoadUint64(&promMetrics.ExecCommandErrors),
		atomic.LoadUint64(&promMetrics.SshConnectionsTotal),
		atomic.LoadUint64(&promMetrics.SshConnectionErrors),
		prometheusLabelValue(getVersion()),
		prometheusLabelValue(CommitID),
		prometheusLabelValue(config.Mode),
		atomic.LoadInt64(&activeWebSockets),
		bridgeState.ActiveSessions,
		logState.Subscribers,
		logState.BufferedLogs,
		logState.PendingLogs,
		logState.LogFileBytes,
	)
}

type serviceStatus struct {
	Status        string       `json:"status"`
	Service       string       `json:"service"`
	Version       string       `json:"version"`
	Commit        string       `json:"commit"`
	BuildTime     string       `json:"build_time"`
	Mode          string       `json:"mode"`
	ListenAddress string       `json:"listen_address"`
	WebSockets    int64        `json:"websocket_connections"`
	Bridge        bridgeStatus `json:"bridge"`
	Logs          logHubStatus `json:"logs"`
}

func buildServiceStatus(bridge *Bridge, config runtimeConfig) serviceStatus {
	return serviceStatus{
		Status:        "ok",
		Service:       "terminal_bridge",
		Version:       getVersion(),
		Commit:        CommitID,
		BuildTime:     BuildTime,
		Mode:          config.Mode,
		ListenAddress: config.address(),
		WebSockets:    atomic.LoadInt64(&activeWebSockets),
		Bridge:        bridge.status(),
		Logs:          logHub.status(),
	}
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		atomic.AddUint64(&promMetrics.LogsCollectErrors, 1)
	}
}

func newHTTPHandler(bridge *Bridge, config runtimeConfig) http.Handler {
	mux := http.NewServeMux()
	liveHandler := func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	}
	readyHandler := func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
	}
	statusHandler := func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, buildServiceStatus(bridge, config))
	}
	metricsHandler := func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		_, _ = io.WriteString(w, getPrometheusMetrics(bridge, config))
	}

	// Service 内部路径与 Ingress 保留前缀后的外部路径同时可用。
	for _, path := range []string{"/health/live", "/terminal-bridge/health/live"} {
		mux.HandleFunc(path, liveHandler)
	}
	for _, path := range []string{"/health/ready", "/terminal-bridge/health/ready"} {
		mux.HandleFunc(path, readyHandler)
	}
	for _, path := range []string{"/status", "/terminal-bridge/status"} {
		mux.HandleFunc(path, statusHandler)
	}
	for _, path := range []string{"/metrics", "/terminal-bridge/metrics"} {
		mux.HandleFunc(path, metricsHandler)
	}
	mux.Handle("/", bridge.corsWebSocketHandler(config))
	return mux
}

// truncateString 截断字符串到指定长度
func truncateString(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}

func initTelemetry(ctx context.Context) (func(context.Context) error, error) {
	endpoint := strings.TrimSpace(os.Getenv("HCI_BRIDGE_OTEL_ENDPOINT"))
	if endpoint == "" {
		otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
		return func(context.Context) error { return nil }, nil
	}
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(endpoint))
	if err != nil {
		return nil, err
	}
	res, err := resource.New(ctx, resource.WithAttributes(
		attribute.String("service.name", "terminal-bridge"),
		attribute.String("service.version", getVersion()),
		attribute.String("service.instance.id", logHub.instanceID),
		attribute.String("deployment.environment", envOrDefault("HCI_DEPLOYMENT_ENVIRONMENT", "local")),
	))
	if err != nil {
		return nil, err
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(res))
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	return provider.Shutdown, nil
}

func main() {
	showVersion := flag.Bool("version", false, "打印版本与 commit 信息后退出")
	mode := flag.String("mode", envOrDefault("HCI_BRIDGE_MODE", desktopMode), "运行模式：desktop 或 cluster")
	listenAddress := flag.String("listen-address", strings.TrimSpace(os.Getenv("HCI_BRIDGE_LISTEN_ADDRESS")), "监听地址；默认 desktop=127.0.0.1，cluster=0.0.0.0")
	port := flag.Int("port", envIntOrDefault("HCI_BRIDGE_PORT", defaultWSPort), "HTTP/WebSocket 监听端口")
	allowedOrigins := flag.String("allowed-origins", strings.TrimSpace(os.Getenv("HCI_BRIDGE_ALLOWED_ORIGINS")), "允许的 Origin，逗号分隔；支持 * 和 same-origin")
	flag.Parse()
	if *showVersion {
		fmt.Printf("terminal_bridge %s (commit: %s, built: %s)\n", getVersion(), CommitID, BuildTime)
		return
	}
	config, err := normalizeRuntimeConfig(*mode, *listenAddress, *port, *allowedOrigins)
	if err != nil {
		log.Fatalf("[Bridge] 配置无效: %v", err)
	}

	bridge := newBridge()
	// 把所有标准库日志重定向为结构化日志并回采（统一可观测性）
	log.SetOutput(bridgeLogWriter{})
	log.SetFlags(0)
	shutdownTelemetry, telemetryErr := initTelemetry(context.Background())
	if telemetryErr != nil {
		log.Printf("[Bridge] ERROR: OpenTelemetry 初始化失败: %v", telemetryErr)
		shutdownTelemetry = func(context.Context) error { return nil }
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdownTelemetry(shutdownCtx); err != nil {
			log.Printf("[Bridge] ERROR: OpenTelemetry 关闭失败: %v", err)
		}
	}()

	server := &http.Server{
		Addr:              config.address(),
		Handler:           newHTTPHandler(bridge, config),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("[Bridge] HCI SSH Bridge 已启动: version=%s commit=%s built=%s mode=%s listen=ws://%s",
		getVersion(), CommitID, BuildTime, config.Mode, config.address())
	log.Printf("[Bridge] Origin 策略已启用: allowed_origins=%s；结构化日志与状态指标已开启", config.AllowedOriginsRaw)

	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("[Bridge] 启动失败: ", err)
	}
}
