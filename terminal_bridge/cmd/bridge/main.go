// terminal_bridge - HCI 排障助手 SSH Bridge
// 桌面架构: Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
// 集群架构: Custom UI (浏览器) → 同源 WebSocket → terminal_bridge Pod → SSH → HCI Linux
// 同一套代码同时构建 Windows 客户端和 Linux 容器镜像。

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"sync/atomic"
	"syscall"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/delivery"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	bruntime "terminal_bridge/internal/shared/runtime"
	"terminal_bridge/internal/shared/telemetry"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/terminal"
	"time"
)

// 版本注入变量：ldflags -X main.Version/CommitID/BuildTime（链接器对 main 包恒用 main 别名，与目录位置无关）。
var (
	Version   = "v2.16.0-dev"
	CommitID  = "unknown" // 构建时通过 -X main.CommitID 注入 git commit
	BuildTime = "unknown" // 构建时通过 -X main.BuildTime 注入构建时间
)

// getVersion 薄包装：真实渲染在 app.GetVersion。
func getVersion() string { return app.GetVersion() }

func main() {
	// R0 装配缝：models/obslog 的注入钩子在此接线（所有权仍归 app/cmd）。
	models.ErrorFieldsFunc = logcollect.LogCollectErrorFields
	models.SendErrorHook = func(msg models.OutMessage, err error) {
		obslog.Emit("ERROR", "ws.send_failed", "WebSocket 发送失败", msg.TraceID, msg.CaseID, "", msg.CustomUI, map[string]any{
			"out_type": msg.Type, "exec_id": msg.ExecID, "error": err.Error(),
		})
		atomic.AddUint64(&app.PromMetrics.LogsCollectErrors, 1)
	}
	obslog.MetricsReplayed = func(n uint64) { atomic.AddUint64(&app.PromMetrics.LogsReplayedTotal, n) }
	obslog.MetricsDropped = func(n uint64) { atomic.AddUint64(&app.PromMetrics.LogsDroppedTotal, n) }
	obslog.MetricsCollectErrors = func(n uint64) { atomic.AddUint64(&app.PromMetrics.LogsCollectErrors, n) }
	obslog.MetricsCollected = func(n uint64) { atomic.AddUint64(&app.PromMetrics.LogsCollectedTotal, n) }
	obslog.VersionFunc = getVersion
	sshx.IsLiveBridgeFunc = app.IsLiveBridge
	sshx.SelfPortFunc = func() int { return app.BridgeSelfPort }
	app.WSHandlerFunc = handle
	app.VersionFunc = func() string { return Version }
	app.CommitIDFunc = func() string { return CommitID }
	app.BuildTimeFunc = func() string { return BuildTime }
	terminal.MetricsExecErrors = func(n uint64) { atomic.AddUint64(&app.PromMetrics.ExecCommandErrors, n) }
	terminal.MetricsVmConsoleOpErrors = func(n uint64) { atomic.AddUint64(&app.PromMetrics.VmConsoleOpErrors, n) }
	showVersion := flag.Bool("version", false, "打印版本与 commit 信息后退出")
	mode := flag.String("mode", bruntime.EnvOrDefault("HCI_BRIDGE_MODE", bruntime.DesktopMode), "运行模式：desktop 或 cluster")
	listenAddress := flag.String("listen-address", strings.TrimSpace(os.Getenv("HCI_BRIDGE_LISTEN_ADDRESS")), "监听地址；默认 desktop=127.0.0.1，cluster=0.0.0.0")
	port := flag.Int("port", bruntime.EnvIntOrDefault("HCI_BRIDGE_PORT", app.DefaultWSPort), "HTTP/WebSocket 监听端口")
	allowedOrigins := flag.String("allowed-origins", strings.TrimSpace(os.Getenv("HCI_BRIDGE_ALLOWED_ORIGINS")), "允许的 Origin，逗号分隔；支持 * 和 same-origin")
	flag.Parse()
	if *showVersion {
		fmt.Printf("terminal_bridge %s (commit: %s, built: %s)\n", getVersion(), CommitID, BuildTime)
		return
	}
	config, err := app.NormalizeRuntimeConfig(*mode, *listenAddress, *port, *allowedOrigins)
	if err != nil {
		log.Fatalf("[Bridge] 配置无效: %v", err)
	}

	bridge := app.NewBridge()
	// app.ProcessStartedAt / app.BridgeSingleton 供退出事件与资源采样读取（生命周期事实只有一处真源）。
	app.ProcessStartedAt = time.Now()
	app.BridgeSingleton = bridge
	// 把所有标准库日志重定向为结构化日志并回采（统一可观测性）
	log.SetOutput(obslog.BridgeLogWriter{})
	log.SetFlags(0)

	// 按运行模式校正落盘策略，并记录启动事件：日志路径必须可自证，
	// 否则排障时无法回答"日志到底写在哪"（工单 Q2026092010235 的直接教训）。
	obslog.DefaultHub.ApplyRuntimeMode(config.Mode)
	// stderr 重定向必须晚于 applyRuntimeMode：cluster 模式的日志目录由 Helm 注入，
	// 提前重定向会把 panic 栈写到 desktop 默认目录。
	app.RedirectStderrToFile()
	// 云端/集群桥没有浏览器给它下发直传令牌：部署期注入了端点与令牌就自行建通道。
	// 建通道时会自动异步补传"水位之后"的全部本地历史（L1，含崩溃窗口与 panic 现场），
	// 启动事件本身走实时 enqueue，不依赖补传时序。
	obslog.LogUpload.BootstrapFromEnv()
	// 5 分钟一条资源采样：Atom 级机器上采样本身也必须廉价（一天约 58KB）。
	app.StartResourceSampler(5 * time.Minute)
	// 空闲会话回收器：WS 关闭只 release 不销毁，超时无人复用的会话由 reaper 兜底回收，
	// 回收间隔取 TTL 的 1/4（最少 15 秒），避免过期会话久留或 Atom 机器频繁抢锁。
	app.StartSessionReaper(bridge, app.SessionIdleTTL())
	// 交付执行器：启动即扫描并恢复未完成作业。执行权在 bridge，
	// 浏览器是否在线不影响；前端只通过 job_status 轮询进度。
	delivery.DeliveryRunnerInstance = delivery.NewDeliveryRunner(bridge)
	delivery.DeliveryRunnerInstance.Start()
	obslog.Emit("INFO", "bridge.startup", "terminal_bridge 已启动", "", "", "", "", map[string]any{
		"version":         getVersion(),
		"commit":          CommitID,
		"built":           BuildTime,
		"mode":            config.Mode,
		"listen":          config.Address(),
		"allowed_origins": config.AllowedOriginsRaw,
		"log_dir":         obslog.DefaultHub.LogDir,
		"log_path":        obslog.DefaultHub.LogPath,
		"log_dir_source":  obslog.DefaultHub.LogDirSource,
		"log_level":       obslog.DefaultHub.LogLevelName(),
		"pid":             os.Getpid(),
		"goos":            runtime.GOOS,
		// 起跑线资源快照：与 resource.sample / bridge.shutdown 对齐，
		// 才能回答"是不是一路涨到崩溃"。
		"goroutines": runtime.NumGoroutine(),
		"sys_mb":     app.CurrentSysMB(),
	})
	// U1：跟踪导出归 telemetry 门面（桌面 noop 零 otel 链接，集群带 tag 完整导出）；
	// 服务身份由装配层注入，门面不认识任何业务域。
	shutdownTelemetry, telemetryErr := telemetry.Init(context.Background(),
		os.Getenv("HCI_BRIDGE_OTEL_ENDPOINT"), getVersion(),
		obslog.DefaultHub.InstanceID, bruntime.EnvOrDefault("HCI_DEPLOYMENT_ENVIRONMENT", "local"))
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

	// 端口自愈：仅默认端口的桌面模式启用 9999↔47324 双端口回退；显式自定义端口或
	// cluster 模式（同源/受管 Service）只绑该端口，冲突即 fail-fast，避免与浏览器
	// 探测契约（只认 9999/47324 两个已知端口）脱节。
	allowBackup := config.Mode == bruntime.DesktopMode && config.Port == app.DefaultWSPort
	listener, resolved, alreadyRunning, listenErr := app.AcquireListener(config, allowBackup)
	if listenErr != nil {
		holders := app.FindPortHolders(config.Port)
		obslog.Emit("ERROR", "bridge.port_conflict", "监听端口不可用，启动失败", "", "", "", "", map[string]any{
			"requested_listen": config.Address(),
			"port":             config.Port,
			"backup_port":      app.BackupWSPort,
			"allow_backup":     allowBackup,
			"holder_pids":      holders,
			"error":            listenErr.Error(),
		})
		if len(holders) > 0 {
			log.Printf("[Bridge] 端口 %d 被进程 PID=%s 占用；可用 'netstat -ano | findstr :%d' (Windows) 或 'lsof -nP -iTCP:%d -sTCP:LISTEN' 定位并处置", config.Port, strings.Join(holders, ","), config.Port, config.Port)
		}
		// 无论是否探到占用 PID 都给出处置指引：WSAEACCES(10013) 场景下 netstat 常常
		// 探不到"能杀的进程"（http.sys 注册、系统/WinNAT 保留段、独占绑定），只给一条
		// netstat 会把人带偏，现场会反复重启同一个端口。
		if runtime.GOOS == "windows" {
			log.Printf("[Bridge] 处置指引：1) netstat -ano | findstr :%d 查占用 PID；2) netsh int ipv4 show excludedportrange protocol=tcp 确认 %d 未被系统/WinNAT 保留；3) 结束残留进程后重试，或用 --port %d 启动", config.Port, config.Port, app.BackupWSPort)
		} else {
			log.Printf("[Bridge] 处置指引：lsof -nP -iTCP:%d -sTCP:LISTEN 定位占用者；或改用 --port %d 启动", config.Port, app.BackupWSPort)
		}
		log.Fatalf("[Bridge] 启动失败: %v", listenErr)
	}
	if alreadyRunning {
		// 同机已有一个存活 Bridge：这是"双击重复启动/上一个没退净"，非故障，幂等退出。
		obslog.Emit("INFO", "bridge.already_running", "检测到本机已有存活的 terminal_bridge，无需重复启动", "", "", "", "", map[string]any{
			"listen": config.Address(),
			"pid":    os.Getpid(),
		})
		log.Printf("[Bridge] 检测到本机 %s 已有存活的 terminal_bridge，本次启动幂等退出（无需重复打开）", config.Address())
		return
	}

	server := &http.Server{
		Addr:              resolved.Address(),
		Handler:           app.NewHTTPHandler(bridge, resolved),
		ReadHeaderTimeout: 10 * time.Second,
	}
	if resolved.Port != config.Port {
		obslog.Emit("WARN", "bridge.backup_port_activated", "主端口被占用，已回退至备用端口", "", "", "", "", map[string]any{
			"primary_port": config.Port,
			"backup_port":  resolved.Port,
			"listen":       resolved.Address(),
		})
	}
	obslog.Emit("INFO", "bridge.listening", "terminal_bridge 监听已就绪", "", "", "", "", map[string]any{
		"listen": resolved.Address(),
		"port":   resolved.Port,
	})
	app.BridgeSelfPort = resolved.Port // 供对端不可达辅助探测回查自身 /health/live（修复 E）
	log.Printf("[Bridge] HCI SSH Bridge 已启动: version=%s commit=%s built=%s mode=%s listen=ws://%s",
		getVersion(), CommitID, BuildTime, resolved.Mode, resolved.Address())
	log.Printf("[Bridge] Origin 策略已启用: allowed_origins=%s；结构化日志与状态指标已开启", resolved.AllowedOriginsRaw)

	// 优雅退出：覆盖 Ctrl+C 与 SIGTERM。
	// Windows 的实际映射（runtime/os_windows.go 的 ctrlHandler）：
	//   CTRL_C / CTRL_BREAK                       → os.Interrupt（日志 signal=interrupt）
	//   CTRL_CLOSE / CTRL_LOGOFF / CTRL_SHUTDOWN  → syscall.SIGTERM（日志 signal=terminated）
	// 即"关闭控制台窗口"是可以被捕获并优雅退出的，日志里会留下 bridge.shutdown；
	// 真正无声消失的只有 TerminateProcess（taskkill /F、任务管理器结束任务、安全软件强杀），
	// 表现为「有 bridge.startup 无 bridge.shutdown」。
	// 旧注释曾写"关闭控制台窗口走 TerminateProcess、收不到信号"，与事实相反，会把人
	// 引向"被强杀"的错误结论（2026-11-20 现场即按此误判过一次）。
	// console_event 进一步区分 SIGTERM 的三种来源，避免把"用户关了窗口"当成异常退出。
	registerConsoleCtrlProbe()
	signalCh := make(chan os.Signal, 1)
	signal.Notify(signalCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		sig := <-signalCh
		extra := map[string]any{
			"signal":          sig.String(),
			"uptime_seconds":  int64(time.Since(app.ProcessStartedAt).Seconds()),
			"active_sessions": bridge.SessionCount(),
		}
		if consoleEvent := lastConsoleCtrlEvent(); consoleEvent != "" {
			extra["console_event"] = consoleEvent
		}
		obslog.Emit("WARN", "bridge.shutdown", "terminal_bridge 收到退出信号，开始优雅退出", "", "", "", "", extra)
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			log.Printf("[Bridge] 优雅退出失败: %v", err)
		}
	}()

	if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("[Bridge] 启动失败: ", err)
	}
}
