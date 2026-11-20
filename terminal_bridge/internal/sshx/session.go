package sshx

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"hash"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/shared/models"
	bruntime "terminal_bridge/internal/shared/runtime"
	"terminal_bridge/internal/shared/telemetry"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

type BoundedCapture struct {
	limit     int
	buffer    []byte
	Total     int64
	hasher    hash.Hash
	Truncated bool
}

func NewBoundedCapture(limit int) *BoundedCapture {
	return &BoundedCapture{limit: limit, hasher: sha256.New()}
}

func (c *BoundedCapture) Write(p []byte) []byte {
	_, _ = c.hasher.Write(p)
	c.Total += int64(len(p))
	remaining := c.limit - len(c.buffer)
	if remaining <= 0 {
		c.Truncated = c.Truncated || len(p) > 0
		return nil
	}
	if len(p) > remaining {
		c.Truncated = true
		p = p[:remaining]
	}
	c.buffer = append(c.buffer, p...)
	return p
}

func (c *BoundedCapture) String() string { return string(c.buffer) }
func (c *BoundedCapture) Bytes() []byte  { return c.buffer }
func (c *BoundedCapture) SHA256() string { return fmt.Sprintf("%x", c.hasher.Sum(nil)) }

type StreamStats struct {
	Filtered     bool
	RawBytes     int64
	rawHasher    hash.Hash
	ScannedLines int64
	KeptLines    int64
	lastByte     byte
}

func NewStreamStats(filtered bool) *StreamStats {
	return &StreamStats{Filtered: filtered, rawHasher: sha256.New()}
}

func (s *StreamStats) RecordRaw(p []byte) {
	if len(p) == 0 {
		return
	}
	_, _ = s.rawHasher.Write(p)
	s.RawBytes += int64(len(p))
	s.ScannedLines += int64(bytes.Count(p, []byte{'\n'}))
	s.lastByte = p[len(p)-1]
}

func (s *StreamStats) Finish() {
	if s.RawBytes > 0 && s.lastByte != '\n' {
		s.ScannedLines++
	}
	if !s.Filtered {
		s.KeptLines = s.ScannedLines
	}
}

func (s *StreamStats) RawSHA256() string { return fmt.Sprintf("%x", s.rawHasher.Sum(nil)) }

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
	// ErrorType 失败语义分类（禁止压缩为笼统 unknown_error）：
	// timeout | nonzero_exit | tool_capability_missing 等，供 bridge_execution_logs 复盘判别。
	ErrorType string
}

// ── SSH 会话 ──────────────────────────────────────────────────────────────────

type SSHSession struct {
	CaseID            string
	Client            *ssh.Client
	ClientConfig      *ssh.ClientConfig
	Address           string
	session           *ssh.Session
	stdin             io.WriteCloser
	Mu                sync.Mutex
	closed            bool
	listenersMu       sync.Mutex
	Listeners         map[string]*ExecListener // key: execID
	Simulation        bool
	ExecutionMode     string
	SimulationBackend string
	// done 在 close() 时关闭，用于让保活 goroutine 立即退出（不靠轮询 closed 标志）。
	Done chan struct{}
	// ownerConn 是建立本会话的浏览器连接 ID，createdAt 是建立时刻。
	// 二者只用于生命周期事件（session.created/destroyed）：没有它们，
	// 事后无法回答"谁建的、活了多久、被谁销毁"（2026-10-27 现场教训）。
	OwnerConn string
	CreatedAt time.Time
	// sftp 是懒加载的只读 SFTP 通道：与交互式 shell 共用同一条 SSH 连接，
	// 但走独立 channel，互不阻塞。由 sftpMu 保护，close() 时一并回收。
	sftpMu sync.Mutex
	Sftp   SftpRemote
	// longTasks 统计占用独立 exec 通道的长任务（交付拉取等）。
	//
	// 为什么必须单独计数：HCI 的交互式 shell 受 TMOUT=600 约束，10 分钟无输入
	// 就退出；而交付拉取 12.8 GiB 需要 20 分钟以上。shell 退出只是**一个 channel**
	// 结束，SSH 连接与 exec 通道其实都还活着——历史实现把它当成整条连接失效并
	// destroySession，于是 24 次交付中断全部停在 duration_seconds=600 上。
	LongTasks atomic.Int32
	// lastBusyAt 记录最近一次业务动作（日志采集/进度/交付/stat/transfer）的时刻（Unix 毫秒）。
	//
	// 为什么需要：长任务只能覆盖"显式包裹"的拉取，而日志采集是一连串短命令
	// （create → progress × N → stat），bridge 无状态、不知道作业何时结束，包裹不成对。
	// 于是采集跑过 10 分钟后 shell 必然被 TMOUT 踢掉，EOF 被当成整条连接失效并
	// destroySession：后续 progress 全部 session_missing，前端只能干等到 60s 超时，
	// 报成误导性的"progress 超时（60s）"（2026-10-28 现场）。
	// 事实是：RunCommandPty 每次都 client.NewSession() 开新 channel，
	// shell 退出只是少了一个终端回显通道，连接与 exec 通道都还能用。
	LastBusyAt atomic.Int64
	// orphaned 表示会话已被摘出路由表（重连覆盖），但底层连接要留给进行中的长任务。
	orphaned atomic.Bool
	// shellRespawns 统计本会话自动重建交互 shell 的次数（上限 MaxShellRespawns）。
	ShellRespawns atomic.Int32
}

// MaxShellRespawns 限制单个会话自动重建交互 shell 的次数。
//
// 为什么要有上限：重建只延长"可用"，不能替代用户重新登录。若用户已离开数小时，
// 每 600s 重建一次等于在节点上无限续开登录 shell，既不礼貌也会累积远端进程。
// 业务活跃窗口（默认 15 分钟）本身会过期，上限只是兜底的第二道闸。
const MaxShellRespawns = 3

// R0 装配缝：bridge 自身存活探测与监听端口归 app/cfg 所有，sshx 不反向依赖。
var (
	IsLiveBridgeFunc func(address string) bool
	SelfPortFunc     func() int
)

// ShellRespawnNotice 是重建成功后写进终端的一行说明。
//
// 必须写进终端输出而不是弹窗：用户切回来时弹窗早已消失，而他要解释的是
// "为什么提示符变了、交互式菜单没了"——这件事只有留在终端里才看得到。
const ShellRespawnNotice = "\r\n[bridge] 设备空闲 10 分钟已自动登出（TMOUT=600）。已在同一条 SSH 连接上重新登录，命令可继续输入；注意当前目录与交互式菜单状态不会保留。\r\n"

// respawnShell 在同一条 SSH 连接上重新开一个交互 shell。
//
// 为什么必须做：HCI 的交互式 shell 空闲 10 分钟必然自动登出（TMOUT=600），
// 而现场最常见的使用方式就是"敲完命令去查资料"。旧实现把 shell 退出当成整条连接失效：
// 终端断开、必须重新认证重连，采集与交付也被连坐（2026-10-28 现场）。
// 不能 unset TMOUT（客户安全策略，清除会触发审计），所以只能"退出后重建"——
// 同一条连接允许再开 channel，代价仅是 shell 内的状态（当前目录、交互菜单）丢失。
func (s *SSHSession) RespawnShell() (io.ReadCloser, error) {
	s.Mu.Lock()
	client, old := s.Client, s.session
	if client == nil || s.closed {
		s.Mu.Unlock()
		return nil, errors.New("SSH 连接已不可用")
	}
	s.Mu.Unlock()

	next, err := client.NewSession()
	if err != nil {
		return nil, fmt.Errorf("重建交互 shell 失败（新建 channel）: %w", err)
	}
	s.Mu.Lock()
	if s.closed {
		s.Mu.Unlock()
		_ = next.Close()
		return nil, errors.New("会话已关闭，放弃重建")
	}
	s.session = next
	s.Mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
	return s.Start()
}

// beginLongTask / endLongTask 成对包裹一个长任务，保护会话不被 shell 退出连坐。
func (s *SSHSession) BeginLongTask() { s.LongTasks.Add(1) }

func (s *SSHSession) EndLongTask() {
	if s.LongTasks.Add(-1) > 0 {
		return
	}
	// 已被重连覆盖摘表的会话，长任务结束后没有任何路由能再找到它：
	// 此时必须自行关闭，否则这条连接会永远挂在远端 shell 上。
	if s.orphaned.Load() {
		go s.Close()
	}
}

func (s *SSHSession) HasLongTask() bool { return s.LongTasks.Load() > 0 }

// markBusy 标记「这条连接刚被业务用过」。
//
// 与 beginLongTask 的区别：长任务是显式的成对包裹（拉取前/后各一次），
// 而日志采集是一串短命令且结束时机未知，只能用时间窗表达"仍在被使用"——
// 窗口到期保护自动失效，不会像漏减的计数那样永久泄漏。
func (s *SSHSession) MarkBusy() {
	if s == nil {
		return
	}
	s.LastBusyAt.Store(time.Now().UnixMilli())
}

// isBusyWithin 判断窗口内是否发生过业务动作；lastBusyAt 为 0 表示从未被用过。
func (s *SSHSession) IsBusyWithin(window time.Duration) bool {
	if s == nil || window <= 0 {
		return false
	}
	last := s.LastBusyAt.Load()
	if last == 0 {
		return false
	}
	return time.Since(time.UnixMilli(last)) < window
}

// isBusy 判断会话是否处于业务活跃窗口内。
func (s *SSHSession) IsBusy() bool { return s.IsBusyWithin(SessionBusyGrace()) }

// busyAgeSeconds 返回距上次业务动作的秒数；从未被用过返回 -1（区分"没用过"与"刚用过"）。
func (s *SSHSession) BusyAgeSeconds() float64 {
	if s == nil {
		return -1
	}
	last := s.LastBusyAt.Load()
	if last == 0 {
		return -1
	}
	return time.Since(time.UnixMilli(last)).Seconds()
}

// markOrphaned 标记会话已被摘表，底层连接由长任务结束后自行关闭。
func (s *SSHSession) MarkOrphaned() { s.orphaned.Store(true) }

// startShellHeartbeat 周期向交互式 shell 注入一个无副作用的 NUL 字节，重置 TMOUT。
//
// 硬约束：不得 unset TMOUT（客户安全策略，清除会触发审计），也不得改用
// `echo` 之类会污染终端回显的心跳——NUL（Ctrl-@）在 bash 里是 set-mark，
// 不产生输出、不改变命令历史，仅用于证明"有人在敲键盘"。
// 返回的 stop 必须由调用方 defer，避免拉取结束后仍持续注入。
func (s *SSHSession) StartShellHeartbeat(caseID string) func() {
	done := make(chan struct{})
	var once sync.Once
	go func() {
		ticker := time.NewTicker(shellHeartbeatInterval)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-s.Done:
				return
			case <-ticker.C:
				s.sendSilent("\x00")
			}
		}
	}()
	logpush.Blog("INFO", "delivery.shell_heartbeat", "拉取期间向交互式 shell 注入保活输入，避免 TMOUT 退出", "", caseID, "", "", map[string]any{
		"interval_seconds": int(shellHeartbeatInterval.Seconds()),
	})
	return func() { once.Do(func() { close(done) }) }
}

func IsSimulationLeaseCredential(msg models.InMessage) bool {
	authType := strings.ToLower(strings.TrimSpace(msg.AuthType))
	return strings.EqualFold(strings.TrimSpace(msg.ExecutionMode), "sim-ssh") &&
		authType == "lease" && strings.HasPrefix(strings.TrimSpace(msg.Password), "htp2.")
}

func NewSSHSession(msg models.InMessage) (*SSHSession, error) {
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
	simulation := IsSimulationLeaseCredential(msg)
	if (authType == "password" || authType == "") && msg.Password != "" && !simulation {
		msg.Password = msg.Password + "sangfornetwork"
	}

	port := msg.Port
	if port == 0 {
		port = 22
	}

	authMethods, err := BuildAuthMethods(msg)
	if err != nil {
		return nil, err
	}

	// JoinHostPort 而非 "%s:%d"：显式 net.Dial 后 IPv6 字面量必须带方括号，否则解析必失败。
	addr := net.JoinHostPort(strings.TrimSpace(msg.Host), strconv.Itoa(port))
	hostKeyCallback, err := BuildHostKeyCallback(addr)
	if err != nil {
		return nil, fmt.Errorf("初始化 SSH 主机指纹校验失败: %w", err)
	}
	clientConfig := &ssh.ClientConfig{
		User:            username,
		Auth:            authMethods,
		HostKeyCallback: hostKeyCallback,
		Timeout:         sshDialTimeout,
	}

	// 显式建连而非 ssh.Dial：Dial 内部使用 net.DialTimeout，无法指定 TCP keepalive 周期。
	// 终端空闲时这条连接没有任何字节流动，防火墙/NAT 老化映射后会静默丢包；
	// 打开 TCP keepalive 至少让内核定期发探测包，让中间设备维持映射。
	rawConn, err := net.DialTimeout("tcp", addr, sshDialTimeout)
	if err != nil {
		return nil, fmt.Errorf("建立 SSH 连接失败: %w", err)
	}
	if tcpConn, ok := rawConn.(*net.TCPConn); ok {
		_ = tcpConn.SetKeepAlive(true)
		_ = tcpConn.SetKeepAlivePeriod(tcpKeepAlivePeriod)
	}
	sshConn, chans, reqs, err := ssh.NewClientConn(rawConn, addr, clientConfig)
	if err != nil {
		_ = rawConn.Close()
		return nil, fmt.Errorf("建立 SSH 连接失败: %w", err)
	}
	client := ssh.NewClient(sshConn, chans, reqs)
	session, err := client.NewSession()
	if err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("创建 SSH 会话失败: %w", err)
	}

	return &SSHSession{
		CaseID:       msg.CaseID,
		Client:       client,
		ClientConfig: clientConfig,
		Address:      addr,
		session:      session,
		Done:         make(chan struct{}),
		Listeners:    make(map[string]*ExecListener),
		Simulation:   simulation,
		ExecutionMode: func() string {
			if simulation {
				return "sim-ssh"
			}
			return ""
		}(),
		SimulationBackend: func() string {
			if simulation {
				return "hci-sim"
			}
			return ""
		}(),
	}, nil
}

var knownHostsMu sync.Mutex

func BuildHostKeyCallback(addr string) (ssh.HostKeyCallback, error) {
	policy := strings.ToLower(bruntime.EnvOrDefault("HCI_BRIDGE_HOST_KEY_POLICY", "accept-new"))
	if policy == "insecure" {
		return ssh.InsecureIgnoreHostKey(), nil
	}
	knownHostsPath := strings.TrimSpace(os.Getenv("HCI_BRIDGE_KNOWN_HOSTS_FILE"))
	if knownHostsPath == "" {
		baseDir := bruntime.EnvOrDefault("HCI_BRIDGE_LOG_DIR", ".")
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

func BuildAuthMethods(msg models.InMessage) ([]ssh.AuthMethod, error) {
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

func (s *SSHSession) Start() (io.ReadCloser, error) {
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

	s.Mu.Lock()
	s.stdin = stdin
	s.Mu.Unlock()

	pipeReader, pipeWriter := io.Pipe()
	var wg sync.WaitGroup
	forward := func(name string, r io.Reader) {
		defer wg.Done()
		if _, copyErr := io.Copy(pipeWriter, r); copyErr != nil && !errors.Is(copyErr, io.ErrClosedPipe) {
			log.Printf("[Bridge] SSH 输出转发异常: case=%s stream=%s err=%v", s.CaseID, name, copyErr)
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

func (s *SSHSession) Send(data string) {
	s.Mu.Lock()
	defer s.Mu.Unlock()
	if s.stdin != nil {
		if _, err := io.WriteString(s.stdin, data); err != nil {
			log.Printf("[Bridge] SSH 输入写入失败: case=%s err=%v", s.CaseID, err)
		}
	}
}

// sendSilent 与 send 相同，但不写日志。
//
// 用于后台心跳：shell 可能已经 TMOUT 退出（此时 stdin 已关闭），
// 而心跳的正常使命就是"在 shell 还活着时别让它退出"，失败不是异常。
func (s *SSHSession) sendSilent(data string) {
	s.Mu.Lock()
	defer s.Mu.Unlock()
	if s.stdin != nil {
		_, _ = io.WriteString(s.stdin, data)
	}
}

// injectCommand 填入命令行但不加换行，等客户回车确认
func (s *SSHSession) InjectCommand(command string) {
	s.Send(command)
}

// ── Exec Marker 监听器管理 ───────────────────────────────────────────────────

func (s *SSHSession) RegisterExecListener(listener *ExecListener) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	s.Listeners[listener.ExecID] = listener
}

func (s *SSHSession) UnregisterExecListener(execID string) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	delete(s.Listeners, execID)
}

func (s *SSHSession) AppendOutput(chunk string) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()
	for _, listener := range s.Listeners {
		maxBytes := bruntime.EnvIntOrDefault("HCI_BRIDGE_EXEC_MAX_OUTPUT_BYTES", 4*1024*1024)
		remaining := maxBytes - listener.OutputBuf.Len()
		if remaining > 0 {
			if len(chunk) > remaining {
				chunk = chunk[:remaining]
			}
			listener.OutputBuf.WriteString(chunk)
		}
	}
}

func (s *SSHSession) CheckMarkers(output string) (*ExecListener, int, bool) {
	s.listenersMu.Lock()
	defer s.listenersMu.Unlock()

	for execID, listener := range s.Listeners {
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

// ClassifyExecFailure 依据命令输出与退出码归类失败语义，禁止压缩为笼统 unknown_error（配合 V-017）。
// 采集镜像工具能力缺失（如 jq 无 ONIGURUMA、命令不存在）归为 tool_capability_missing，
// 目标盘符在内核中不存在/已摘除归为 device_absent（§4），其余非零退出归为 nonzero_exit（exit 0 返回空串表示成功）。
func ClassifyExecFailure(exitCode int, stdoutAndStderr string) string {
	if exitCode == 0 {
		return ""
	}
	lowered := strings.ToLower(stdoutAndStderr)
	switch {
	case strings.Contains(lowered, "no such device"),
		strings.Contains(lowered, "cannot open /dev/"),
		strings.Contains(lowered, "unable to open /dev/"),
		strings.Contains(lowered, "not a block device"),
		strings.Contains(lowered, "no such disk"),
		strings.Contains(lowered, "设备不存在"),
		strings.Contains(lowered, "磁盘不存在"):
		return "device_absent"
	case strings.Contains(lowered, "oniguruma"),
		strings.Contains(lowered, "regex could not be compiled"),
		strings.Contains(lowered, "invalid oniguruma"),
		strings.Contains(lowered, "command not found"),
		strings.Contains(lowered, "not found"),
		strings.Contains(lowered, "no such file or directory"):
		return "tool_capability_missing"
	default:
		return "nonzero_exit"
	}
}

// devPathRe 提取命令中引用的 /dev/<name> 设备令牌（盘符或分区）。
var devPathRe = regexp.MustCompile(`/dev/([A-Za-z0-9_.]+)`)

// DeviceAbsentFromProbe 依据原始命令引用的 /dev 设备与现场 lsblk 整盘清单交叉比对，
// 判别“命令引用的盘符在内核中不存在/已摘除”。lsblk -dno NAME 只列整盘，故用“整盘名是引用令牌前缀”
// 兼容分区（sdb1 命中 sdb、nvme0n1p2 命中 nvme0n1）。仅当命令确实引用了 /dev 设备、
// 且全部引用盘符均未命中时判定缺失；任一命中即返回非缺失，避免误报。
func DeviceAbsentFromProbe(command, lsblkOutput string) (absent bool, referenced []string) {
	matches := devPathRe.FindAllStringSubmatch(command, -1)
	names := strings.Fields(lsblkOutput)
	for _, m := range matches {
		token := m[1]
		if token == "" {
			continue
		}
		referenced = append(referenced, token)
		for _, d := range names {
			if d != "" && strings.HasPrefix(token, d) {
				return false, referenced // 至少一个引用盘符现场存在 → 非设备缺失
			}
		}
	}
	return len(referenced) > 0, referenced
}

// ProbeLevel 根据探测结果选择日志级别（成功 INFO，失败 WARN）。
func ProbeLevel(ok bool) string {
	if ok {
		return "INFO"
	}
	return "WARN"
}

// probeRemote 在已建立的 SSH 连接上执行一条只读探测命令，返回 (合并输出, 退出码)。
// 探测失败不得 panic，统一以非零退出码返回供上层判别。
func probeRemote(client *ssh.Client, command string) (string, int) {
	session, err := client.NewSession()
	if err != nil {
		return "new_session_failed: " + err.Error(), -1
	}
	defer session.Close()
	out, err := session.CombinedOutput(command)
	code := 0
	if err != nil {
		if ee, ok := err.(*ssh.ExitError); ok {
			code = ee.ExitStatus()
		} else {
			code = -1
		}
	}
	return string(out), code
}

// RunUnreachabilityProbes 在命令超时/对端建会话失败/SSH 等待失败时，触发一组廉价辅助探测，
// 各自落 logpush.BlogContext(event=probe.*) 与 OTel span event，为复盘区分“网络不可达 / 认证失败 /
// 设备不存在 / 工具缺失 / 命令超时”提供可判别依据（修复 E）。
// 探测以独立 goroutine 异步运行、自带短超时，绝不阻塞主结果回传；任何探测异常都安全降级为一条记录。
func RunUnreachabilityProbes(ctx context.Context, s *SSHSession, req models.ExecRequestContext, artifactID string) {
	go func() {
		// 主 exec ctx 在 ExecCommandIsolated 返回时会被 cancel，探测 goroutine 必须使用去耦合的
		// 派生上下文（保留 trace 值、忽略取消），并自带总体超时，否则 DialContext 会因取消立即失败。
		probeCtx, probeCancel := context.WithTimeout(context.WithoutCancel(ctx), 20*time.Second)
		defer probeCancel()
		ctx = probeCtx

		defer func() {
			if r := recover(); r != nil {
				logpush.BlogContext(ctx, "WARN", "probe.error", "辅助探测异常", req, map[string]any{
					"panic": fmt.Sprintf("%v", r), "artifact_id": artifactID,
				})
			}
		}()

		span := telemetry.SpanFromContext(ctx)

		// 1. node_ip:22 TCP 可达性
		tcpTarget := s.Address
		dialer := net.Dialer{Timeout: 3 * time.Second}
		tcpConn, tcpErr := dialer.DialContext(ctx, "tcp", tcpTarget)
		tcpOK := tcpErr == nil
		tcpDetail := "reachable"
		if !tcpOK {
			tcpDetail = tcpErr.Error()
		} else {
			_ = tcpConn.Close()
		}
		span.AddEvent("probe.tcp_reachability",
			telemetry.Bool("probe.tcp.ok", tcpOK),
			telemetry.String("probe.tcp.target", tcpTarget),
		)
		logpush.BlogContext(ctx, ProbeLevel(tcpOK), "probe.tcp_reachability", "TCP:22 可达性探测", req, map[string]any{
			"target": tcpTarget, "ok": tcpOK, "detail": tcpDetail, "artifact_id": artifactID,
		})
		if !tcpOK {
			logpush.BlogContext(ctx, "ERROR", "probe.conclusion", "辅助探测结论：网络不可达", req, map[string]any{
				"conclusion": "network_unreachable", "artifact_id": artifactID,
			})
			return
		}

		// 2. SSH banner/kex 握手（克隆配置并收紧超时，避免污染主会话或长时间阻塞）
		cfg := *s.ClientConfig
		cfg.Timeout = 5 * time.Second
		sshClient, sshErr := ssh.Dial("tcp", tcpTarget, &cfg)
		sshOK := sshErr == nil
		sshDetail := "handshake_ok"
		authFailed := sshErr != nil && strings.Contains(strings.ToLower(sshErr.Error()), "unable to authenticate")
		if !sshOK {
			sshDetail = sshErr.Error()
		}
		span.AddEvent("probe.ssh_handshake", telemetry.Bool("probe.ssh.ok", sshOK))
		logpush.BlogContext(ctx, ProbeLevel(sshOK), "probe.ssh_handshake", "SSH 握手探测", req, map[string]any{
			"target": tcpTarget, "ok": sshOK, "auth_failed": authFailed, "detail": sshDetail, "artifact_id": artifactID,
		})
		if !sshOK {
			conclusion := "ssh_unreachable"
			if authFailed {
				conclusion = "auth_failed"
			}
			logpush.BlogContext(ctx, "ERROR", "probe.conclusion", "辅助探测结论", req, map[string]any{
				"conclusion": conclusion, "artifact_id": artifactID,
			})
			return
		}
		defer sshClient.Close()

		// 3. bridge 自身 /health/live（区分“bridge 自身故障”与“目标节点故障”）
		bridgeSelfPort := 0
		if SelfPortFunc != nil {
			bridgeSelfPort = SelfPortFunc()
		}
		if bridgeSelfPort > 0 {
			selfAddr := net.JoinHostPort("127.0.0.1", strconv.Itoa(bridgeSelfPort))
			bridgeOK := IsLiveBridgeFunc != nil && IsLiveBridgeFunc(selfAddr)
			logpush.BlogContext(ctx, ProbeLevel(bridgeOK), "probe.bridge_health", "Bridge 自身存活探测", req, map[string]any{
				"target": selfAddr, "ok": bridgeOK, "artifact_id": artifactID,
			})
		}

		// 4. 工具能力探测：acli / jq ONIGURUMA（佐证“工具缺失”而非“设备不可达”）
		acliOut, acliCode := probeRemote(sshClient, "command -v acli >/dev/null 2>&1 && acli --version 2>&1 || echo __ACLI_ABSENT__")
		acliOK := acliCode == 0 && !strings.Contains(acliOut, "__ACLI_ABSENT__")
		logpush.BlogContext(ctx, ProbeLevel(acliOK), "probe.tool_capability.acli", "acli 工具能力探测", req, map[string]any{
			"ok": acliOK, "exit_code": acliCode, "detail": TruncateString(acliOut, 256), "artifact_id": artifactID,
		})

		jqOut, jqCode := probeRemote(sshClient, `jq --version 2>&1 || echo __JQ_ABSENT__; echo 1 | jq -R 'test("a")' 2>&1 || true`)
		jqOK := jqCode == 0 && !strings.Contains(jqOut, "__JQ_ABSENT__")
		jqHasOniguruma := strings.Contains(strings.ToLower(jqOut), "oniguruma")
		logpush.BlogContext(ctx, ProbeLevel(jqOK), "probe.tool_capability.jq", "jq 工具能力/ONIGURUMA 探测", req, map[string]any{
			"ok": jqOK, "oniguruma": jqHasOniguruma, "exit_code": jqCode, "detail": TruncateString(jqOut, 256), "artifact_id": artifactID,
		})

		// 5. 最小现场探测：lsblk 盘符清单，佐证“设备已摘除/不存在”
		lsblkOut, lsblkCode := probeRemote(sshClient, "lsblk -dno NAME 2>&1 || echo __LSBLK_ABSENT__")
		lsblkOK := lsblkCode == 0 && !strings.Contains(lsblkOut, "__LSBLK_ABSENT__")
		logpush.BlogContext(ctx, ProbeLevel(lsblkOK), "probe.field.lsblk", "现场盘符探测", req, map[string]any{
			"ok": lsblkOK, "exit_code": lsblkCode, "devices": TruncateString(lsblkOut, 256), "artifact_id": artifactID,
		})

		// 5.5 设备存在性判别：命令引用的 /dev 盘符若全部不在现场 lsblk 整盘清单中 → device_absent（§4/§5）。
		if lsblkOK {
			if devAbsent, referenced := DeviceAbsentFromProbe(req.Command, lsblkOut); devAbsent {
				span.AddEvent("probe.device_absent",
					telemetry.String("probe.device.referenced", strings.Join(referenced, ",")),
				)
				logpush.BlogContext(ctx, "WARN", "probe.conclusion", "辅助探测结论：设备不存在/已摘除", req, map[string]any{
					"conclusion": "device_absent", "referenced_devices": referenced,
					"lsblk_devices": TruncateString(lsblkOut, 256), "artifact_id": artifactID,
				})
				return
			}
		}

		logpush.BlogContext(ctx, "INFO", "probe.conclusion", "辅助探测结论", req, map[string]any{
			"conclusion": "network_reachable_tools_checked", "acli_ok": acliOK, "jq_ok": jqOK,
			"jq_oniguruma": jqHasOniguruma, "lsblk_ok": lsblkOK, "artifact_id": artifactID,
		})
	}()
}

func (s *SSHSession) ExecCommand(command, execID string, timeout time.Duration) <-chan ExecResult {
	resultChan := make(chan ExecResult, 1)

	listener := &ExecListener{
		ExecID:     execID,
		StartTime:  time.Now(),
		OutputBuf:  &strings.Builder{},
		ResultChan: resultChan,
	}

	s.RegisterExecListener(listener)

	go func() {
		select {
		case <-time.After(timeout):
			s.listenersMu.Lock()
			if l, ok := s.Listeners[execID]; ok {
				output := l.OutputBuf.String()
				delete(s.Listeners, execID)
				s.listenersMu.Unlock()
				resultChan <- ExecResult{Output: output, ExitCode: -1, Timeout: true, ErrorType: "timeout"}
			} else {
				s.listenersMu.Unlock()
			}
		case <-resultChan:
		}
	}()

	s.Send(command)

	return resultChan
}

// PTY 固定 40x160：与日志收集等 acli 插件命令的表格输出宽度匹配，避免长行被截断。
func (s *SSHSession) Wait() error {
	s.Mu.Lock()
	session := s.session
	s.Mu.Unlock()
	if session == nil {
		return nil
	}
	return session.Wait()
}

func (s *SSHSession) IsClosed() bool {
	s.Mu.Lock()
	defer s.Mu.Unlock()
	return s.closed
}

func (s *SSHSession) Close() {
	s.Mu.Lock()
	if s.closed {
		s.Mu.Unlock()
		return
	}
	s.closed = true
	// 关闭 done 让保活 goroutine 立刻退出；closed 已保证这里只会执行一次。
	if s.Done != nil {
		close(s.Done)
	}
	stdin := s.stdin
	s.stdin = nil
	session := s.session
	s.session = nil
	client := s.Client
	s.Client = nil
	s.Mu.Unlock()

	// SFTP 通道必须先于 SSH 连接关闭：它的生命周期挂在同一条连接上，
	// 反过来关会让 sftp.Client 在已失效的 transport 上等超时。
	s.sftpMu.Lock()
	remote := s.Sftp
	s.Sftp = nil
	s.sftpMu.Unlock()
	if remote != nil {
		_ = remote.Close()
	}

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

// SshKeepAliveInterval 返回应用层保活间隔。
// 可用 HCI_BRIDGE_SSH_KEEPALIVE_SECONDS 调整；设为 <=0 表示关闭应用层探测（仅保留 TCP keepalive）。
func SshKeepAliveInterval() time.Duration {
	return time.Duration(bruntime.EnvIntOrDefault("HCI_BRIDGE_SSH_KEEPALIVE_SECONDS", sshKeepAliveIntervalDefault)) * time.Second
}

// startKeepAlive 启动 SSH 应用层保活探测。
//
// 为什么必须自己做：Bridge 与 HCI 节点之间通常隔着防火墙/NAT/负载均衡，
// 终端空闲时这条 SSH 连接上一个字节都不流，映射老化后连接会被静默丢弃；
// 而 x/crypto/ssh 自身不做任何探测，半开连接要等到下一次业务写入才暴露，
// 现场表现为「终端看着还在、一敲命令就卡住、日志收集报会话不存在」。
//
// 探测失败即 close()：关闭会让 pty 读循环立刻 EOF，走既有的 onExit 清理链路
// （移除会话 + 通知前端），不新增第二条清理路径，避免状态双写。
func (s *SSHSession) StartKeepAlive(interval time.Duration) {
	// done 为空的是测试里手工构造的会话：没有退出信号，就不启动 goroutine，避免泄漏。
	if interval <= 0 || s.Done == nil {
		return
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-s.Done:
				return
			case <-ticker.C:
				if err := s.ProbeKeepAlive(); err != nil {
					logpush.Blog("WARN", "ssh.keepalive_failed", "SSH 保活探测失败，会话判定失效", "", s.CaseID, "", "", map[string]any{
						"address": s.Address, "error": err.Error(),
					})
					s.Close()
					return
				}
			}
		}
	}()
}

// probeKeepAlive 发送 keepalive@openssh.com 全局请求。
//
// 判定口径：只有传输层错误才算连接失效。服务端不认识该请求会回 REQUEST_FAILURE，
// SendRequest 返回 wantReply=false 与 nil error，属于正常「链路活着」的证据。
func (s *SSHSession) ProbeKeepAlive() error {
	s.Mu.Lock()
	client := s.Client
	s.Mu.Unlock()
	if client == nil {
		return errors.New("ssh client 已关闭")
	}
	result := make(chan error, 1)
	go func() {
		_, _, err := client.SendRequest("keepalive@openssh.com", true, nil)
		result <- err
	}()
	select {
	case err := <-result:
		return err
	case <-s.Done:
		return errors.New("会话已关闭")
	case <-time.After(sshKeepAliveProbeTimeout):
		// 半开连接上写操作可能永久阻塞：超时即判定链路失效，不能让保活自身卡死。
		return fmt.Errorf("保活探测 %.0fs 无响应", sshKeepAliveProbeTimeout.Seconds())
	}
}

func BuildSSHError(err error) (string, string) {
	detail := summarizeSSHDetail(err.Error())
	if msg, _, matched := classifySSHFailure(err.Error()); matched {
		return msg, detail
	}
	return "SSH 连接失败", detail
}

// ── 容器命令包装 ─────────────────────────────────────────────────────────────

// WrapContainerCommand 根据 container 类型包装命令
// container="host" 或空 → 原样执行
// container="vs-cp-manager" 等 → container_exec -n <container> -c "<command>"
// simulation=true（已认证的 sim-ssh 租约会话）→ 一律下发「内层裸命令」，绝不 container_exec 包裹、绝不加 PATH 前缀：
//
//	真机非交互 SSH 宿主机 PATH 不含 /sf/bin:/sf/sbin，真机必须靠该前缀找到 acli/smartctl；
//	而 hci-sim 按 argv 字符串匹配 fixture 路由、从不 exec 真实二进制：
//	  1) PATH 前缀含 shell 操作符 ';'，会触发 hci-sim 受限 lexer「禁止 shell 操作符 ';'」→ policy_denied(exit 126)；
//	  2) 容器步骤服务端已把命令包成自包含派发壳（HCI_CONTAINER=...; HCI_USER_COMMAND='<真实命令>'; ...case...esac），
//	     若再按 container 名 container_exec 包裹，hci-sim 只看到一个 tool=container_exec 的畸形 argv → fixture_not_found(exit 127)。
//	hci-sim 无真实容器、其 lease container 恒为 host，忠实仿真语义就是执行派发壳内层的用户命令，故解出 HCI_USER_COMMAND 裸发。
func WrapContainerCommand(command, container string, simulation bool) string {
	if simulation {
		// 仿真会话：解出容器派发壳内层用户命令（无派发壳则原样），裸命令交 hci-sim 按 argv 匹配。
		return extractContainerUserCommand(command)
	}
	container = strings.TrimSpace(container)
	if container == "" || container == "host" {
		trimmed := strings.TrimSpace(command)
		if strings.HasPrefix(trimmed, "container_exec ") && !strings.Contains(trimmed, " -d") {
			command = trimmed + " -d"
		}
		if !strings.Contains(command, "export PATH=") && (strings.Contains(command, "acli") || strings.Contains(command, "smartctl") || strings.Contains(command, "container_exec")) {
			return "export PATH=\"$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin\"; " + command
		}
		return command
	}
	// 对命令中的单引号做转义
	escaped := strings.ReplaceAll(command, "'", "'\\''")
	return fmt.Sprintf("container_exec -n %s -c '%s' -d", container, escaped)
}

// extractContainerUserCommand 从服务端容器执行派发壳（见 agent-service container_exec.py）中
// 解出 `HCI_USER_COMMAND=<shell 词>` 的 <shell 词>（按 shell 单/双引号与反斜杠转义规则解析一层引号）。
// 非派发壳（不含该标记）原样返回；内层仍是派发壳则递归再解一层（防御多层包裹）。
// 仅用于仿真会话：hci-sim 无容器、只按裸 argv 匹配 fixture，派发壳含 ';' 等操作符根本无法通过其受限 lexer。
func extractContainerUserCommand(command string) string {
	const marker = "HCI_USER_COMMAND="
	idx := strings.Index(command, marker)
	if idx < 0 {
		return command
	}
	inner, ok := ScanFirstShellToken(command[idx+len(marker):])
	if !ok {
		return command
	}
	if strings.Contains(inner, marker) {
		return extractContainerUserCommand(inner)
	}
	return inner
}

// ScanFirstShellToken 从 s 起始解析第一个 shell 词：支持单引号（内部原样）、
// 双引号（内部 \\ 转义）与裸反斜杠转义；顶层（引号外）遇空白或 shell 操作符
// ; | & < > ( ) 即视为该词结束（与 hci-sim 受限 lexer 一致的分词边界）。返回去引号后的内容与是否成功。
func ScanFirstShellToken(s string) (string, bool) {
	var b strings.Builder
	runes := []rune(s)
	i := 0
	// 跳过前导空白
	for i < len(runes) && isShellSpace(runes[i]) {
		i++
	}
	if i >= len(runes) {
		return "", false
	}
	started := false
	for i < len(runes) {
		ch := runes[i]
		// 顶层空白或 shell 操作符：已起词即为词尾，未起词则空 token
		if isShellSpace(ch) {
			if started {
				return b.String(), true
			}
			i++
			continue
		}
		if isShellOperator(ch) {
			if started {
				return b.String(), true
			}
			return "", false
		}
		switch ch {
		case '\'':
			started = true
			i++
			for i < len(runes) && runes[i] != '\'' {
				b.WriteRune(runes[i])
				i++
			}
			if i >= len(runes) {
				return "", false // 单引号未闭合
			}
			i++ // 跳过闭合单引号；后续若紧跟 shlex 的 \' 转义或相邻引号段，由循环顶的反斜杠/单引号分支自然继续
		case '"':
			started = true
			i++
			for i < len(runes) && runes[i] != '"' {
				// shell 双引号内仅对 $ ` " \ 与新行转义；其余反斜杠按字面保留
				if runes[i] == '\\' && i+1 < len(runes) && strings.ContainsRune("$`\"\\\n", runes[i+1]) {
					i++
				}
				b.WriteRune(runes[i])
				i++
			}
			if i >= len(runes) {
				return "", false
			}
			i++
		case '\\':
			started = true
			if i+1 < len(runes) {
				b.WriteRune(runes[i+1])
				i += 2
			} else {
				i++
			}
		default:
			started = true
			b.WriteRune(ch)
			i++
		}
	}
	if !started || b.Len() == 0 {
		return "", false
	}
	return b.String(), true
}

// isShellSpace 判断空白字符（仅处理命令传输常见的空白，不含 exotic unicode 空白）。
func isShellSpace(ch rune) bool {
	return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r'
}

// isShellOperator 判断引号外的 shell 操作符（与 hci-sim fixture.Lex 拒绝的字符集保持分词语义一致）。
func isShellOperator(ch rune) bool {
	return strings.ContainsRune(";|&<>()", ch)
}

// ── 虚拟机控制台截图固定操作通道（vm_console_op）─────────────────────────────
//
// 设计依据：docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md §3.2/§3.3/§5.2/§6.1。
//
//  1. 本通道只接受代码枚举的固定操作 capture_baseline 与 wake_down_key，不接受任何
//     自由文本 command、路径或键名；全部 argv 由代码常量表构造，host_node_id/vm_id/
//     capture_id 在进入拼接前完成严格字符校验（fail-closed）。
//  2. 执行一律走隔离 SSH 连接（与 ssh_exec_process 的 ExecCommandIsolated 同款通道），
//     不经过 PTY marker 通道；目标为宿主机直连（host 直连，不做容器包装）。
//  3. 截图文件经固定 base64 命令读取，BoundedCapture 上限 16MiB；base64 解码在 Go
//     本地进行，解码后计算 SHA-256；原始 PPM 经 HTTP 直传平台制品服务，WS 只回
//     vm_console_result 元数据，绝不降级 base64 over WS。
//  4. PLATFORM_ARTIFACT_URL 未配置 → fail-closed artifact_upload_disabled。

func (s *SSHSession) RunQuickCommand(ctx context.Context, cmd string) (string, int, error) {
	stdout, stderr, exitCode, err := s.RunQuickCommandStreams(ctx, cmd)
	// 历史契约：stdout 为空时用 stderr 兜底当输出，调用方只看一个字符串。
	if stdout == "" {
		stdout = stderr
	}
	return stdout, exitCode, err
}

// runQuickCommandStreams 与 runQuickCommand 同源，但把 stdout 与 stderr 分开返回。
//
// 为什么必须分开：压缩包存在性探测要能区分两种"都不成功"——
// 「节点上没有这个命令」（exit 126/127，stderr 形如 "stat: not found"）与
// 「节点上有命令但没有这个文件」（stderr 形如 "No such file or directory"）。
// 二者的结论完全相反，合并成一条字符串后不可分辨，只能一律当成"包被删了"，
// 这正是 Q2026102900165 现场"包在盘上却被判不存在"的根因。
func (s *SSHSession) RunQuickCommandStreams(ctx context.Context, cmd string) (string, string, int, error) {
	session, err := s.Client.NewSession()
	if err != nil {
		return "", "", -1, err
	}
	defer session.Close()

	var stdoutBuf, stderrBuf bytes.Buffer
	session.Stdout = &stdoutBuf
	session.Stderr = &stderrBuf

	done := make(chan error, 1)
	go func() {
		done <- session.Run(cmd)
	}()

	select {
	case <-ctx.Done():
		_ = session.Signal(ssh.SIGKILL)
		return "", "", -1, ctx.Err()
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
		return strings.TrimSpace(stdoutBuf.String()), strings.TrimSpace(stderrBuf.String()), exitCode, err
	}
}

// runQuickCommandBytes 与 runQuickCommandStreams 同构，但保留 stdout 原始字节。
//
// 为什么要单独一份：证据侧需要把 zip 尾部（含二进制 EOCD / 中央目录）取回本地，
// runQuickCommandStreams 会 TrimSpace 并当字符串处理，二进制数据会被破坏。
func (s *SSHSession) RunQuickCommandBytes(ctx context.Context, cmd string) ([]byte, string, int, error) {
	session, err := s.Client.NewSession()
	if err != nil {
		return nil, "", -1, err
	}
	defer session.Close()

	var stdoutBuf, stderrBuf bytes.Buffer
	session.Stdout = &stdoutBuf
	session.Stderr = &stderrBuf

	done := make(chan error, 1)
	go func() {
		done <- session.Run(cmd)
	}()

	select {
	case <-ctx.Done():
		_ = session.Signal(ssh.SIGKILL)
		return nil, "", -1, ctx.Err()
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
		return stdoutBuf.Bytes(), strings.TrimSpace(stderrBuf.String()), exitCode, err
	}
}

// uploadStream 已删除（S1 死代码清理）。
//
// 它曾是"cat > tmp && mv"的无校验版本：丢弃 stdout、不验 size/digest、
// 失败不清 tmp。全仓唯一引用是 acli_package.go 里的一句注释，实际调用
// 早已换成 uploadAcliPackage。保留一份"看起来能用但没有校验"的写入函数，
// 等于给未来的人留一个绕过完整性协议的入口，因此直接删掉。
// 唯一写入实现：remote.WriteFile（先校验再 mv，失败只删 tmp）。

// ParseAcliVersion 从版本探测输出里取版本号与构建时间。
//
// S1 收口：解析唯一实现在 remote.ParseVersionOutput。acli 特有的
// `__ACLI_BIN__=` / `__ACLI_ABSENT__` 契约由 BuildAcliVersionCommand 产出，
// 原语只负责如实解析（包括"未安装"这一结论）。

// AgeSeconds 返回会话存活秒数；未记录建立时间时返回 0。
func AgeSeconds(s *SSHSession) float64 {
	if s == nil || s.CreatedAt.IsZero() {
		return 0
	}
	return time.Since(s.CreatedAt).Seconds()
}

// 会话销毁原因：写入 session.destroyed.reason，供云端按原因聚合与告警。
const (
	SessionReasonWSClosed         = "ws_closed"         // 拥有它的浏览器连接关闭（连坐清理）
	SessionReasonReconnect        = "reconnect"         // 同键再次 ssh_connect 覆盖
	SessionReasonRemoteEOF        = "remote_eof"        // 远端 shell 退出
	SessionReasonClientDisconnect = "client_disconnect" // 前端显式 ssh_disconnect
	SessionReasonKeepAlive        = "keepalive_failed"  // 保活探测判定链路失效
	SessionReasonIdleTTL          = "idle_ttl"          // 空闲租约超时，被 reaper 回收
)

// SessionHostAddress 给出发起节点的 IP：节点清单不可得时的兜底收敛目标。
func SessionHostAddress(s *SSHSession, fallback string) string {
	if trimmed := strings.TrimSpace(fallback); trimmed != "" {
		return trimmed
	}
	if s != nil {
		if host, _, err := net.SplitHostPort(s.Address); err == nil && host != "" {
			return host
		}
		if s.Address != "" {
			return s.Address
		}
	}
	return "unknown"
}
