package obslog

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/shared/models"
	bruntime "terminal_bridge/internal/shared/runtime"
	"terminal_bridge/internal/shared/telemetry"
	"time"

	"golang.org/x/net/websocket"
)

type LogEntry struct {
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
	// StderrOffset 仅 bridge.stderr 包装条目使用：该行在 bridge-stderr.log 中的结束字节偏移。
	// L3 的字节水位靠它推进，必须随 outbox 落盘往返，故不用 json:"-"；普通条目恒为 0（omitempty 不改变 wire 形状）。
	StderrOffset int64 `json:"stderr_offset,omitempty"`
}

// bridgeSubscriber 代表一个已连接的 Custom-UI 浏览器（一个回采订阅者）。
type bridgeSubscriber struct {
	conn     *websocket.Conn
	customUI string
	CaseID   string
	Mu       sync.Mutex
	pending  []LogEntry // 断网/重启期间的待重传缓冲（有界）
	flushing bool
	// bornSeq 订阅建立时的全局 seq 水位：早于它的都是「本连接订阅之前」发生的事件，
	// 从未实时推给过本连接，正是需要回放的那批（bridge.startup 等）。
	bornSeq uint64
	// replayedThrough 已回放到本连接的最大 seq（只对 < bornSeq 的历史段有意义），
	// 保证同一连接多次 setCase（ssh_connect → resume）只增量回放。
	// 两个字段的所有读写都在 h.Mu 下进行。
	replayedThrough uint64
}

// LogHub 全局日志中枢：结构化落盘 + 环形缓冲 + 多订阅者回采。
type LogHub struct {
	Mu              sync.Mutex
	Seq             uint64
	cap             int
	Ring            []LogEntry // 全局有界环形缓冲，供晚加入/重连回放
	subs            map[*websocket.Conn]*bridgeSubscriber
	logFile         *os.File
	LogPath         string
	LogDir          string // 本地日志目录（desktop 模式默认为用户桌面）
	LogDirSource    string // 目录来源：env:HCI_BRIDGE_LOG_DIR / desktop / fallback:* / disabled:*
	minLevel        int    // 日志级别阈值（0=DEBUG 1=INFO 2=WARN 3=ERROR）
	logFileBytes    int64
	maxLogFileBytes int64
	InstanceID      string
}

var DefaultHub = newLogHub()

func deterministicUUID(seed string) string {
	sum := sha256.Sum256([]byte(seed))
	sum[6] = (sum[6] & 0x0f) | 0x50
	sum[8] = (sum[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", sum[0:4], sum[4:6], sum[6:8], sum[8:10], sum[10:16])
}

func DeterministicArtifactID(execID string) string { return deterministicUUID("artifact:" + execID) }
func deterministicEventID(instanceID string, seq uint64) string {
	return deterministicUUID(fmt.Sprintf("event:%s:%d", instanceID, seq))
}

func newBridgeInstanceID() string {
	hostname, _ := os.Hostname()
	return deterministicUUID(fmt.Sprintf("%s:%d:%d", hostname, os.Getpid(), time.Now().UnixNano()))
}

// 注入缝（R0 结构性 seams）：metrics 所有权归 app、版本归 cmd，obslog 只调用。
var (
	MetricsReplayed      func(n uint64)
	MetricsDropped       func(n uint64)
	MetricsCollectErrors func(n uint64)
	MetricsCollected     func(n uint64)
	VersionFunc          func() string
)

func serviceVersion() string {
	if VersionFunc != nil {
		return VersionFunc()
	}
	return "dev"
}

func newLogHub() *LogHub {
	h := &LogHub{
		cap:             5000,
		subs:            make(map[*websocket.Conn]*bridgeSubscriber),
		maxLogFileBytes: int64(bruntime.EnvIntOrDefault("HCI_BRIDGE_LOG_MAX_BYTES", 64*1024*1024)),
		InstanceID:      newBridgeInstanceID(),
		minLevel:        parseLogLevel(bruntime.EnvOrDefault("HCI_BRIDGE_LOG_LEVEL", "INFO")),
	}

	// 本地持久化（重启回放）：best-effort。
	// 目录优先级：HCI_BRIDGE_LOG_DIR > desktop 模式默认桌面目录 > 不落盘。
	// desktop 模式默认落盘的原因：云端回采依赖浏览器在线，断网/页面关闭/Diagnostics 中断时
	// 桥侧证据会整体丢失（工单 Q2026092010235 正是因此无法定因），本地全量日志是兜底证据源。
	dir, source := "", ""
	if envDir := strings.TrimSpace(os.Getenv("HCI_BRIDGE_LOG_DIR")); envDir != "" {
		dir, source = envDir, "env:HCI_BRIDGE_LOG_DIR"
	} else if !desktopLogDisabled() && bruntime.EnvOrDefault("HCI_BRIDGE_MODE", bruntime.DesktopMode) == bruntime.DesktopMode {
		dir, source = resolveDesktopLogDir()
	} else {
		source = "disabled:mode_cluster"
	}
	h.LogDirSource = source
	if dir != "" {
		h.enableLocalLogFile(dir, source)
	}
	return h
}

// parseLogLevel 把级别名转换为权重（DEBUG<INFO<WARN<ERROR），未知级别按 INFO 处理。
func parseLogLevel(level string) int {
	switch strings.ToUpper(strings.TrimSpace(level)) {
	case "DEBUG":
		return 0
	case "WARN", "WARNING":
		return 2
	case "ERROR":
		return 3
	default:
		return 1
	}
}

// droppedLogEmittedAt 记录上一次 log.dropped 的写入时间（纳秒），用于限频避免日志风暴。
var droppedLogEmittedAt int64

// markDroppedLogEmitted 以 5 秒为窗口限频，返回本次是否允许写入 log.dropped。
func markDroppedLogEmitted() bool {
	now := time.Now().UnixNano()
	last := atomic.LoadInt64(&droppedLogEmittedAt)
	if last != 0 && now-last < int64(5*time.Second) {
		return false
	}
	return atomic.CompareAndSwapInt64(&droppedLogEmittedAt, last, now)
}

// desktopLogDisabled 判断是否通过 HCI_BRIDGE_LOG_TO_DESKTOP=false 显式关闭桌面落盘。
func desktopLogDisabled() bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv("HCI_BRIDGE_LOG_TO_DESKTOP")))
	return value == "false" || value == "0" || value == "no"
}

// levelEnabled 判断该级别是否满足阈值，用于降低本地日志与回采噪声。
func (h *LogHub) levelEnabled(level string) bool {
	if h.minLevel <= 0 {
		return true
	}
	return parseLogLevel(level) >= h.minLevel
}

// logLevelName 返回当前阈值名称，供启动日志与 /status 自证。
func (h *LogHub) LogLevelName() string {
	switch h.minLevel {
	case 0:
		return "DEBUG"
	case 2:
		return "WARN"
	case 3:
		return "ERROR"
	default:
		return "INFO"
	}
}

// resolveDesktopLogDir 解析 desktop 模式默认日志目录：优先用户桌面（兼容中文系统"桌面"），
// 桌面不可用时回退应用数据目录，再回退可执行文件同级 logs 目录。
// 返回目录与来源标识；桌面候选要求目录已存在，避免在用户目录凭空创建中文目录。
func resolveDesktopLogDir() (string, string) {
	var desktopCandidates []string
	if runtime.GOOS == "windows" {
		if userProfile := strings.TrimSpace(os.Getenv("USERPROFILE")); userProfile != "" {
			desktopCandidates = append(desktopCandidates,
				filepath.Join(userProfile, "Desktop"),
				filepath.Join(userProfile, "桌面"),
			)
		}
	}
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		desktopCandidates = append(desktopCandidates,
			filepath.Join(home, "Desktop"),
			filepath.Join(home, "桌面"),
		)
	}
	for _, candidate := range desktopCandidates {
		if info, err := os.Stat(candidate); err == nil && info.IsDir() {
			return filepath.Join(candidate, "HCI-TerminalBridge-Logs"), "desktop"
		}
	}

	// 回退：应用数据目录 → 可执行文件同级目录
	var fallbacks []string
	if runtime.GOOS == "windows" {
		if localAppData := strings.TrimSpace(os.Getenv("LOCALAPPDATA")); localAppData != "" {
			fallbacks = append(fallbacks, filepath.Join(localAppData, "HCI", "TerminalBridge", "Logs"))
		}
	}
	if cacheDir, err := os.UserCacheDir(); err == nil && cacheDir != "" {
		fallbacks = append(fallbacks, filepath.Join(cacheDir, "HCI", "TerminalBridge", "Logs"))
	}
	if exe, err := os.Executable(); err == nil {
		fallbacks = append(fallbacks, filepath.Join(filepath.Dir(exe), "logs"))
	}
	for _, candidate := range fallbacks {
		if err := os.MkdirAll(candidate, 0o755); err == nil {
			return candidate, "fallback:appdata"
		}
	}
	return "", "disabled:no_writable_dir"
}

// enableLocalLogFile 打开本地日志文件并回放上一次运行的日志（进程重启续接）。
func (h *LogHub) enableLocalLogFile(dir, source string) {
	if dir == "" {
		return
	}
	h.Mu.Lock()
	if h.logFile != nil {
		h.Mu.Unlock()
		return
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		h.LogDirSource = "disabled:mkdir_failed"
		h.Mu.Unlock()
		return
	}
	logPath := filepath.Join(dir, h.logFileNameLocked())
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		h.LogDirSource = "disabled:open_failed"
		h.Mu.Unlock()
		return
	}
	h.LogDir, h.LogPath, h.LogDirSource = dir, logPath, source
	h.logFile = f
	if info, statErr := f.Stat(); statErr == nil {
		h.logFileBytes = info.Size()
	}
	h.Mu.Unlock()

	// 回放上一次运行的日志（锁外执行，避免 publish 重入死锁）
	if replayed := h.replayLocalLogFile(dir, logPath); replayed > 0 {
		log.Printf("[Bridge] 进程重启回放: 从 %s 加载 %d 条历史日志", logPath, replayed)
	}
}

// disableLocalFile 关闭本地落盘（cluster 模式不应写桌面目录）。
func (h *LogHub) disableLocalFile() {
	h.Mu.Lock()
	defer h.Mu.Unlock()
	if h.logFile != nil {
		_ = h.logFile.Close()
		h.logFile = nil
	}
	h.LogPath, h.LogDir = "", ""
}

// applyRuntimeMode 在 flag 解析后校正落盘策略：env 显式指定优先，
// desktop 模式启用桌面落盘，cluster 模式关闭桌面落盘。
func (h *LogHub) ApplyRuntimeMode(mode string) {
	if strings.TrimSpace(os.Getenv("HCI_BRIDGE_LOG_DIR")) != "" {
		return
	}
	if mode == bruntime.DesktopMode && h.LogDirSource == "disabled:mode_cluster" && !desktopLogDisabled() {
		dir, source := resolveDesktopLogDir()
		h.enableLocalLogFile(dir, source)
		return
	}
	if mode == bruntime.ClusterMode && strings.HasPrefix(h.LogDirSource, "desktop") {
		h.disableLocalFile()
		h.LogDirSource = "disabled:mode_cluster"
	}
}

// logFileNameLocked 生成本次运行的日志文件名：bridge-<日期>-<实例前8位>.log
func (h *LogHub) logFileNameLocked() string {
	suffix := h.InstanceID
	if len(suffix) > 8 {
		suffix = suffix[:8]
	}
	return fmt.Sprintf("bridge-%s-%s.log", time.Now().Format("20060102"), suffix)
}

// replayLocalLogFile 回放上一次运行的日志文件到环形缓冲，返回回放条数。
func (h *LogHub) replayLocalLogFile(dir, currentPath string) int {
	path := latestBridgeLogFile(dir, currentPath)
	if path == "" {
		return 0
	}
	f, err := os.Open(path)
	if err != nil {
		return 0
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	replayCount := 0
	for scanner.Scan() {
		var entry LogEntry
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			continue
		}
		h.Mu.Lock()
		if len(h.Ring) >= h.cap {
			h.Ring = h.Ring[1:]
		}
		h.Ring = append(h.Ring, entry)
		if entry.Seq > h.Seq {
			h.Seq = entry.Seq
		}
		h.Mu.Unlock()
		replayCount++
	}
	if replayCount > 0 {
		if MetricsReplayed != nil {
			MetricsReplayed(uint64(replayCount))
		}
	}
	return replayCount
}

// latestBridgeLogFile 选取目录内最近修改的 bridge-*.log（排除当前运行文件）。
func latestBridgeLogFile(dir, exclude string) string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return ""
	}
	best := ""
	var bestMod time.Time
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasPrefix(name, "bridge-") || !strings.HasSuffix(name, ".log") {
			continue
		}
		path := filepath.Join(dir, name)
		if path == exclude {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		if best == "" || info.ModTime().After(bestMod) {
			best, bestMod = path, info.ModTime()
		}
	}
	return best
}

func (h *LogHub) rotateLogFileLocked(nextEntryBytes int64) error {
	if h.logFile == nil || h.maxLogFileBytes <= 0 || h.logFileBytes+nextEntryBytes <= h.maxLogFileBytes {
		return nil
	}
	if err := h.logFile.Close(); err != nil {
		return err
	}
	h.logFile = nil
	rotatedPath := h.LogPath + ".1"
	_ = os.Remove(rotatedPath)
	if err := os.Rename(h.LogPath, rotatedPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	file, err := os.OpenFile(h.LogPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	h.logFile = file
	h.logFileBytes = 0
	return nil
}

func (h *LogHub) publish(e LogEntry) LogEntry {
	h.Mu.Lock()
	// 级别阈值过滤：低于阈值的日志不落盘、不回采（HCI_BRIDGE_LOG_LEVEL 控制）
	if !h.levelEnabled(e.Level) {
		h.Mu.Unlock()
		return e
	}
	h.Seq++
	e.Seq = h.Seq
	e.Timestamp = time.Now().UTC().Format(time.RFC3339Nano)
	e.Type = "bridge_log" // 前端监听 type==="bridge_log" 的必备字段，统一在此注入
	e.BridgeInstanceID = h.InstanceID
	e.EventID = deterministicEventID(h.InstanceID, h.Seq)
	e.ServiceName = "terminal-bridge"
	e.ServiceVersion = serviceVersion()
	e.DeploymentEnvironment = bruntime.EnvOrDefault("HCI_DEPLOYMENT_ENVIRONMENT", "local")
	if len(h.Ring) >= h.cap {
		h.Ring = h.Ring[1:]
		if MetricsDropped != nil {
			MetricsDropped(1)
		}
	}
	h.Ring = append(h.Ring, e)
	if h.logFile != nil {
		if b, err := json.Marshal(e); err == nil {
			line := append(b, '\n')
			if rotateErr := h.rotateLogFileLocked(int64(len(line))); rotateErr != nil {
				if MetricsCollectErrors != nil {
					MetricsCollectErrors(1)
				}
			} else if written, writeErr := h.logFile.Write(line); writeErr != nil {
				if MetricsCollectErrors != nil {
					MetricsCollectErrors(1)
				}
			} else {
				h.logFileBytes += int64(written)
			}
		} else {
			if MetricsCollectErrors != nil {
				MetricsCollectErrors(1)
			}
		}
	}
	eligibleSubscribers := make([]*bridgeSubscriber, 0, len(h.subs))
	for _, sub := range h.subs {
		if e.CaseID == "" || sub.CaseID == "" || sub.CaseID == e.CaseID {
			h.enqueue(sub, e)
			eligibleSubscribers = append(eligibleSubscribers, sub)
		}
	}
	h.Mu.Unlock()

	// P2-8: 更新 Prometheus 指标
	if MetricsCollected != nil {
		MetricsCollected(1)
	}

	// 直传入队必须发生在解锁之后：enqueue 自己持锁，且不能与 publish 的 h.Mu 嵌套。
	// 无令牌时它是空操作（只落本地文件），因此对旧前端完全无感。
	LogUpload.enqueue(e)

	// 实时推送：publish 后立即异步 flush，保证 bridge_log 实时送达前端
	// （此前只在 setCase 时 flush，导致 ssh.connected 之后的日志全部滞留 pending queue）
	for _, sub := range eligibleSubscribers {
		go h.flushSubscriber(sub)
	}
	return e
}

func (h *LogHub) enqueue(sub *bridgeSubscriber, e LogEntry) {
	sub.Mu.Lock()
	if len(sub.pending) >= 2000 {
		sub.pending = sub.pending[1:]
		sub.Mu.Unlock()
		// 待重传队列溢出：显式记录 log.dropped，避免日志无声消失。
		// 注意：enqueue 处于 publish 持锁路径上，此处只能异步记录，否则 publish 重入死锁。
		if MetricsDropped != nil {
			MetricsDropped(1)
		}
		if markDroppedLogEmitted() {
			go Emit("WARN", "log.dropped", "回采队列溢出，丢弃最早一条日志", e.TraceID, e.CaseID, e.NodeIP, "", map[string]any{
				"reason": "pending_overflow", "event_id": e.EventID, "seq": e.Seq,
			})
		}
		return
	}
	sub.pending = append(sub.pending, e)
	sub.Mu.Unlock()
}

// flushSubscriber 把待重传缓冲经 bridge_log 推给浏览器；连接断开则保留待下次重传。
func (h *LogHub) flushSubscriber(sub *bridgeSubscriber) {
	sub.Mu.Lock()
	if sub.flushing {
		sub.Mu.Unlock()
		return
	}
	sub.flushing = true
	for {
		if len(sub.pending) == 0 {
			sub.flushing = false
			sub.Mu.Unlock()
			return
		}
		e := sub.pending[0]
		sub.pending = sub.pending[1:]
		sub.Mu.Unlock()

		e.CustomUI = sub.customUI // 按订阅者归属 custom_ui，自动回采到对应后台
		b, err := json.Marshal(e)
		if err != nil {
			sub.Mu.Lock()
			continue
		}
		if err := models.SendWebSocketRaw(sub.conn, string(b)); err != nil {
			sub.Mu.Lock()
			if len(sub.pending) >= 2000 {
				sub.pending = sub.pending[:1999]
			}
			sub.pending = append([]LogEntry{e}, sub.pending...)
			sub.flushing = false
			sub.Mu.Unlock()
			return
		}
		sub.Mu.Lock()
	}
}

func (h *LogHub) AddSubscriber(conn *websocket.Conn, customUI string) *bridgeSubscriber {
	sub := &bridgeSubscriber{conn: conn, customUI: customUI}
	h.Mu.Lock()
	// 记录订阅水位：这是区分"实时推送过"与"订阅前发生、只能靠回放补"的唯一依据。
	sub.bornSeq = h.Seq + 1
	h.subs[conn] = sub
	h.Mu.Unlock()
	return sub
}

// emptyCaseReplayWindow 无工单上下文事件的回放时间窗。
//
// 为什么需要：`bridge.startup` / `listening` / `WebSocket_请求` / `connected` /
// `disconnected` 这些事件没有 case_id，且往往发生在浏览器订阅之前（进程刚起、
// 探测连接、真正连接建立的那一瞬），publish 时没有订阅者接收。历史回放条件只有
// `e.CaseID == caseID`，把它们全部排除 —— 于是"Bridge 起不来/连不上"这类最需要
// 证据的场景，库里一条记录都没有（实测一次会话漏采 14 条）。这里按时间窗补回。
const emptyCaseReplayWindow = 5 * time.Minute

// logEntryBefore 判断事件时间是否早于 cutoff；时间解析失败按"未超窗"处理，宁可多回放。
func logEntryBefore(e LogEntry, cutoff time.Time) bool {
	ts, err := time.Parse(time.RFC3339Nano, e.Timestamp)
	if err != nil {
		return false
	}
	return ts.Before(cutoff)
}

// setCase 标记订阅者归属工单，并回放该工单近期日志（重连/晚加入可重传）。
//
// 回放口径：本工单全部事件 + 窗口内无工单上下文的事件，且跳过已投递过的 seq。
// 库侧有 (bridge_instance_id, seq) 与 event_id 唯一索引 + 入库 ON CONFLICT DO NOTHING，
// 因此重复回放是幂等的，不会产生脏数据。
// BindCase 实现 app.LogCaseSubscriber 接口缝：把「归属工单」能力暴露给
// WS 派发上下文，装配层不必认识私有订阅者类型。
func (s *bridgeSubscriber) BindCase(caseID string) { DefaultHub.SetCase(s, caseID) }

// BoundCase 返回当前归属工单（app.LogCaseSubscriber 接口缝的读侧；字段已名 CaseID，方法避让）。
func (s *bridgeSubscriber) BoundCase() string { return s.CaseID }

func (h *LogHub) SetCase(sub *bridgeSubscriber, caseID string) {
	h.Mu.Lock()
	if sub.CaseID != caseID {
		// 切换归属工单时重置历史游标，避免新工单的历史被旧游标挡掉。
		sub.replayedThrough = 0
	}
	sub.CaseID = caseID
	replay := h.replayForCaseLocked(sub, caseID, time.Now().UTC())
	// 回放入队必须仍在 h.Mu 内：enqueue 会推进 deliveredThrough，该字段与 publish
	// 共享同一把锁（锁序恒为 h.Mu → sub.Mu），放到锁外会引入数据竞争。
	for _, e := range replay {
		h.enqueue(sub, e)
	}
	h.Mu.Unlock()

	// P2-8: 更新回放指标
	if len(replay) > 0 {
		if MetricsReplayed != nil {
			MetricsReplayed(uint64(len(replay)))
		}
	}

	go h.flushSubscriber(sub)
}

// replayForCaseLocked 挑选需要回放给该订阅者的条目（调用方必须已持 h.Mu）。
//
// 独立成函数是为了能被单测直接覆盖——回放口径是"日志缺失"类缺陷的唯一裁决点，
// 只靠集成验证太脆。
func (h *LogHub) replayForCaseLocked(sub *bridgeSubscriber, caseID string, now time.Time) []LogEntry {
	if caseID == "" || sub == nil {
		return nil
	}
	cutoff := now.Add(-emptyCaseReplayWindow)
	var replay []LogEntry
	var scannedThrough uint64
	for _, e := range h.Ring {
		// 订阅之后产生的事件已经实时推送过，重复回放只会制造重复入库；
		// 订阅之前的事件才是"没人收到过"的那批，按工单归属与时间窗筛选。
		if e.Seq >= sub.bornSeq {
			continue
		}
		if e.Seq <= sub.replayedThrough {
			continue
		}
		if e.Seq > scannedThrough {
			scannedThrough = e.Seq
		}
		if e.CaseID == caseID || (e.CaseID == "" && !logEntryBefore(e, cutoff)) {
			replay = append(replay, e)
		}
	}
	// 窗口外的历史也一并跳过，避免每次 setCase 都重新扫一遍同一批陈旧事件。
	if scannedThrough > sub.replayedThrough {
		sub.replayedThrough = scannedThrough
	}
	return replay
}

func (h *LogHub) RemoveSubscriber(conn *websocket.Conn) {
	h.Mu.Lock()
	delete(h.subs, conn)
	h.Mu.Unlock()
}

type LogHubStatus struct {
	BufferedLogs int    `json:"buffered_logs"`
	Subscribers  int    `json:"subscribers"`
	PendingLogs  int    `json:"pending_logs"`
	LogFileBytes int64  `json:"log_file_bytes"`
	LogDir       string `json:"log_dir"`
	LogPath      string `json:"log_path"`
	LogDirSource string `json:"log_dir_source"`
	LogLevel     string `json:"log_level"`
}

func (h *LogHub) Status() LogHubStatus {
	h.Mu.Lock()
	defer h.Mu.Unlock()

	status := LogHubStatus{
		BufferedLogs: len(h.Ring),
		Subscribers:  len(h.subs),
		LogFileBytes: h.logFileBytes,
		// 暴露落盘位置：排障与日志上传入口都要据此定位本地文件
		LogDir:       h.LogDir,
		LogPath:      h.LogPath,
		LogDirSource: h.LogDirSource,
		LogLevel:     DefaultHub.LogLevelName(),
	}
	for _, sub := range h.subs {
		sub.Mu.Lock()
		status.PendingLogs += len(sub.pending)
		sub.Mu.Unlock()
	}
	return status
}

// BridgeLogWriter 把标准库 log 输出重定向为结构化日志并回采，从而零改造捕获全部既有 log.Printf。
type BridgeLogWriter struct{}

func (BridgeLogWriter) Write(p []byte) (int, error) {
	line := RedactSensitiveText(strings.TrimRight(string(p), "\n"))
	level := "INFO"
	upper := strings.ToUpper(line)
	switch {
	case strings.Contains(upper, "ERROR"):
		level = "ERROR"
	case strings.Contains(upper, "WARNING"), strings.Contains(upper, "WARN"):
		level = "WARN"
	}
	if !DefaultHub.levelEnabled(level) {
		return len(p), nil
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
	e := DefaultHub.publish(LogEntry{Level: level, Service: "terminal_bridge", Event: event, Message: msg})
	b, err := json.Marshal(e)
	if err != nil {
		return len(p), nil
	}
	_, err = os.Stdout.Write(append(b, '\n'))
	return len(p), err
}

// Emit 记录带上下文（trace/case/node/custom_ui）的结构化日志并回采。
func Emit(level, event, msg, traceID, caseID, nodeIP, customUI string, extra map[string]any) {
	if !DefaultHub.levelEnabled(level) {
		return
	}
	e := LogEntry{
		Level:    normalizeLogLevel(level),
		Service:  "terminal_bridge",
		Event:    event,
		Message:  RedactSensitiveText(msg),
		TraceID:  traceID,
		CaseID:   caseID,
		NodeIP:   nodeIP,
		CustomUI: customUI,
		Extra:    sanitizeExtra(extra),
	}
	e = DefaultHub.publish(e)
	if b, err := json.Marshal(e); err == nil {
		os.Stdout.Write(append(b, '\n'))
	}
}

func EmitContext(ctx context.Context, level, event, message string, req models.ExecRequestContext, extra map[string]any) {
	if !DefaultHub.levelEnabled(level) {
		return
	}
	info := telemetry.TraceInfo(ctx)
	e := LogEntry{
		Level: normalizeLogLevel(level), Service: "terminal_bridge", Event: event,
		Message: message, TraceID: info.TraceID, SpanID: info.SpanID,
		TraceFlags: fmt.Sprintf("%02x", info.TraceFlags), Traceparent: TraceparentFromContext(ctx),
		CaseID: req.CaseID, NodeIP: req.NodeIP, CustomUI: req.CustomUI,
		ConversationID: req.ConversationID, ExecID: req.ExecID, ToolCallID: req.ToolCallID,
		Extra: sanitizeExtra(extra),
	}
	e = DefaultHub.publish(e)
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

func RedactSensitiveText(value string) string {
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
			sanitized[key] = RedactSensitiveText(textValue)
		} else {
			sanitized[key] = value
		}
	}
	return sanitized
}

func RedactCommand(command string) string {
	redacted := RedactSensitiveText(command)
	if len(redacted) > 2048 {
		return redacted[:2048] + "...(截断)"
	}
	return redacted
}

func CommandSHA256(command string) string {
	sum := sha256.Sum256([]byte(command))
	return fmt.Sprintf("%x", sum[:])
}

func TraceparentFromContext(ctx context.Context) string {
	return telemetry.Traceparent(ctx)
}

func NormalizeTraceparentForLegacyGo(traceparent string) string {
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

func ContextFromMessage(msg models.InMessage) context.Context {
	// 传播解析归 telemetry 门面（纯 W3C，零 otel 依赖）：obslog 是证据通道，
	// 桌面剥离 otel 后日志仍须携带平台下发的 trace 关联（设计 §2.6 裁决）。
	return telemetry.Extract(context.Background(), NormalizeTraceparentForLegacyGo(msg.Traceparent), msg.Tracestate)
}

// ── WebSocket Handler ─────────────────────────────────────────────────────────

// SessionKey 生成多节点会话的唯一键
