package app

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync/atomic"
	"terminal_bridge/internal/logpush"

	"golang.org/x/net/websocket"
)

func (b *Bridge) corsWebSocketHandler(config runtimeConfig) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if !config.OriginAllowed(origin, r.Host) {
			logpush.Blog("WARNING", "websocket.origin_rejected", "拒绝非授权 Origin 的 WebSocket 请求", "", "", "", CustomUIHost(origin), map[string]any{
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
			WSHandlerFunc(b, ws, origin)
		})
		wsHandler.ServeHTTP(w, r)
	})
}

// CustomUIHost 从完整 Origin 中提取可识别的标识（去 scheme），用于日志标签与回采关联。
func CustomUIHost(origin string) string {
	if origin == "" || origin == "*" {
		return "unknown"
	}
	o := origin
	if i := strings.Index(o, "://"); i >= 0 {
		o = o[i+3:]
	}
	return o
}

// P2-8: Prometheus 指标（简化实现 - 无外部依赖）
var (
	activeWebSockets int64
)

func prometheusLabelValue(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	value = strings.ReplaceAll(value, `"`, `\"`)
	return strings.ReplaceAll(value, "\n", `\n`)
}

func getPrometheusMetrics(bridge *Bridge, config runtimeConfig) string {
	bridgeState := bridge.status()
	logState := logpush.DefaultHub.Status()
	return fmt.Sprintf(`# HELP bridge_logs_collected_total Total logs collected
# TYPE bridge_logs_collected_total counter
bridge_logs_collected_total %d

# HELP bridge_logs_collect_errors_total Total log collection errors
# TYPE bridge_logs_collect_errors_total counter
bridge_logs_collect_errors_total %d

# HELP bridge_logs_replayed_total Total logs replayed on resume/restart
# TYPE bridge_logs_replayed_total counter
bridge_logs_replayed_total %d

# HELP bridge_logs_dropped_total Total logs dropped by ring buffer or pending queue
# TYPE bridge_logs_dropped_total counter
bridge_logs_dropped_total %d

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

# HELP bridge_vm_console_ops_total Total VM console fixed operations executed
# TYPE bridge_vm_console_ops_total counter
bridge_vm_console_ops_total %d

# HELP bridge_vm_console_op_errors_total Total VM console fixed operation errors
# TYPE bridge_vm_console_op_errors_total counter
bridge_vm_console_op_errors_total %d

# HELP bridge_sftp_downloads_total Total remote file downloads started via SFTP panel
# TYPE bridge_sftp_downloads_total counter
bridge_sftp_downloads_total %d

# HELP bridge_sftp_download_errors_total Total remote file downloads that ended in error
# TYPE bridge_sftp_download_errors_total counter
bridge_sftp_download_errors_total %d

# HELP bridge_process_up Whether the terminal_bridge process is serving requests
# TYPE bridge_process_up gauge
bridge_process_up 1

# HELP bridge_build_info Build and runtime information for terminal_bridge
# TYPE bridge_build_info gauge
bridge_build_info{version="%s",commit="%s",mode="%s"} 1

# HELP bridge_websocket_connections_active Current browser WebSocket connections
# TYPE bridge_websocket_connections_active gauge
bridge_websocket_connections_active %d

# HELP bridge_ssh_sessions_active Current active SSH Sessions
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
		atomic.LoadUint64(&PromMetrics.LogsCollectedTotal),
		atomic.LoadUint64(&PromMetrics.LogsCollectErrors),
		atomic.LoadUint64(&PromMetrics.LogsReplayedTotal),
		atomic.LoadUint64(&PromMetrics.LogsDroppedTotal),
		atomic.LoadUint64(&PromMetrics.ExecCommandsTotal),
		atomic.LoadUint64(&PromMetrics.ExecCommandErrors),
		atomic.LoadUint64(&PromMetrics.SshConnectionsTotal),
		atomic.LoadUint64(&PromMetrics.SshConnectionErrors),
		atomic.LoadUint64(&PromMetrics.VmConsoleOpsTotal),
		atomic.LoadUint64(&PromMetrics.VmConsoleOpErrors),
		atomic.LoadUint64(&PromMetrics.SftpDownloadsTotal),
		atomic.LoadUint64(&PromMetrics.SftpDownloadErrors),
		prometheusLabelValue(GetVersion()),
		prometheusLabelValue(CommitIDFunc()),
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
	Status        string               `json:"status"`
	Service       string               `json:"service"`
	Version       string               `json:"version"`
	Commit        string               `json:"commit"`
	BuildTime     string               `json:"build_time"`
	Mode          string               `json:"mode"`
	ListenAddress string               `json:"listen_address"`
	WebSockets    int64                `json:"websocket_connections"`
	Bridge        bridgeStatus         `json:"bridge"`
	Logs          logpush.LogHubStatus `json:"logs"`
	// logpush.LogUpload 暴露日志直传状态：现场第一句话往往是"到底有没有直传"，
	// 没有这个字段就只能靠猜（浏览器关掉后是否还有日志进来）。
	LogUpload map[string]any `json:"log_upload"`
}

func buildServiceStatus(bridge *Bridge, config runtimeConfig) serviceStatus {
	return serviceStatus{
		Status:        "ok",
		Service:       "terminal_bridge",
		Version:       GetVersion(),
		Commit:        CommitIDFunc(),
		BuildTime:     BuildTimeFunc(),
		Mode:          config.Mode,
		ListenAddress: config.Address(),
		WebSockets:    atomic.LoadInt64(&activeWebSockets),
		Bridge:        bridge.status(),
		Logs:          logpush.DefaultHub.Status(),
		LogUpload:     logpush.LogUploadStats(),
	}
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		atomic.AddUint64(&PromMetrics.LogsCollectErrors, 1)
	}
}

func NewHTTPHandler(bridge *Bridge, config runtimeConfig) http.Handler {
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

// buildDate 取版本时间的日期部分（YYYY-MM-DD）；长度不足时原样返回。
