package main

import (
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/logpush"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/shared/models"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/crypto/ssh"

	"terminal_bridge/internal/shared/remote"
)

const (
	// vm_console 固定操作枚举（设计文档 §5.2），未知 operation 一律 Fail Closed。
	vmConsoleOpCaptureBaseline = "capture_baseline"
	vmConsoleOpWakeDownKey     = "wake_down_key"

	// 固定路径与命令常量（设计文档 §3.2/§6.1）：vtpsh 可执行名、Monitor URI 模板、
	// screendump 临时目录全部固定在代码中，平台确认后只改常量。
	vmConsoleVtpshBinary = "vtpsh"
	vmConsoleCaptureDir  = "/sf/data/local/hci-diagnosis"

	vmConsoleMaxCaptureBytes  = 16 * 1024 * 1024 // 解码后 PPM 字节上限
	vmConsoleMaxBase64Bytes   = 16 * 1024 * 1024 // base64 读取输出的有界捕获上限
	vmConsoleMaxStderrBytes   = 64 * 1024        // 单步 stderr 捕获上限（仅用于错误摘要）
	vmConsoleProbeMaxAttempts = 10               // 文件探测有界轮询次数（总等待有上限）
	vmConsoleProbeInterval    = time.Second      // 文件探测轮询间隔
	vmConsoleCleanupTimeout   = 15 * time.Second // 操作预算耗尽后清理步骤的独立短超时
	vmConsoleUploadTimeout    = 60 * time.Second // PPM 直传独立超时
	vmConsoleMaxTimeoutSecs   = 60               // 单次操作总预算上限（秒）
	vmConsoleDefaultTimeout   = 60               // 未指定 timeout 时的默认预算（秒）

	// upload_status 枚举
	vmConsoleUploadOK            = "uploaded"
	vmConsoleUploadDisabled      = "artifact_upload_disabled"
	vmConsoleUploadFailed        = "upload_failed"
	vmConsoleUploadNotApplicable = "not_applicable"

	// error_type 枚举（失败分类不得压缩为同一种"采集失败"）
	vmConsoleErrTargetInvalid    = "target_invalid"
	vmConsoleErrOperationInvalid = "operation_invalid"
	vmConsoleErrSessionMissing   = "session_missing"
	vmConsoleErrSessionCreate    = "session_creation_failed"
	vmConsoleErrCaptureFailed    = "capture_failed"
	vmConsoleErrCaptureMissing   = "capture_file_missing"
	vmConsoleErrReadFailed       = "capture_read_failed"
	vmConsoleErrDecodeFailed     = "capture_decode_failed"
	vmConsoleErrTooLarge         = "capture_too_large"
	vmConsoleErrWakeFailed       = "wake_failed"
	vmConsoleErrUploadFailed     = "upload_failed"
	vmConsoleErrTimeout          = "timeout"
	vmConsoleErrCleanupFailed    = "cleanup_failed"
)

var (
	// Inventory 验证后的宿主机节点标识：字母数字开头，仅允许 . _ - 与字母数字。
	vmConsoleHostNodeIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	// VMID：纯数字字符串。
	vmConsoleVMIDPattern = regexp.MustCompile(`^[0-9]{1,20}$`)
	// capture_id：标准 UUID 形态（服务端生成）。
	vmConsoleCaptureIDPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
)

// vmConsoleRequest 是通过入口校验后的固定操作请求，字段均不可再参与 shell 语义。
type vmConsoleRequest struct {
	CaseID         string
	NodeIP         string
	ExecID         string
	CaptureID      string
	Operation      string
	HostNodeID     string
	VMID           string
	TimeoutSeconds int
	TraceID        string
	Traceparent    string
	Tracestate     string
	ConversationID string
	CustomUI       string
}

// parseVMConsoleRequest 在入口即完成全部校验，返回 (请求, error_type)。
// error_type 为空表示校验通过；任何校验失败都不会触发命令执行。
// 校验顺序：operation 白名单（operation_invalid）→ 目标与标识字符校验（target_invalid）。
func parseVMConsoleRequest(msg models.InMessage, customUI string) (vmConsoleRequest, string) {
	req := vmConsoleRequest{
		CaseID:         strings.TrimSpace(msg.CaseID),
		NodeIP:         strings.TrimSpace(msg.NodeIP),
		ExecID:         strings.TrimSpace(msg.ExecID),
		CaptureID:      strings.TrimSpace(msg.CaptureID),
		Operation:      strings.TrimSpace(msg.Operation),
		HostNodeID:     strings.TrimSpace(msg.HostNodeID),
		VMID:           strings.TrimSpace(msg.VMID),
		TraceID:        msg.TraceID,
		Traceparent:    msg.Traceparent,
		Tracestate:     msg.Tracestate,
		ConversationID: msg.ConversationID,
		CustomUI:       customUI,
		TimeoutSeconds: vmConsoleTimeoutSeconds(msg.Timeout),
	}
	// operation 白名单：只允许两个固定操作，未知值独立分类为 operation_invalid。
	if req.Operation != vmConsoleOpCaptureBaseline && req.Operation != vmConsoleOpWakeDownKey {
		return req, vmConsoleErrOperationInvalid
	}
	// 目标字段逐项严格校验：任一不满足 → target_invalid，不执行任何命令。
	if req.CaseID == "" || req.ExecID == "" || req.NodeIP == "" {
		return req, vmConsoleErrTargetInvalid
	}
	if !vmConsoleHostNodeIDPattern.MatchString(req.HostNodeID) {
		return req, vmConsoleErrTargetInvalid
	}
	if !vmConsoleVMIDPattern.MatchString(req.VMID) {
		return req, vmConsoleErrTargetInvalid
	}
	if !vmConsoleCaptureIDPattern.MatchString(req.CaptureID) {
		return req, vmConsoleErrTargetInvalid
	}
	return req, ""
}

// vmConsoleTimeoutSeconds 把 1-60 秒范围外的 timeout 收敛为安全默认值。
func vmConsoleTimeoutSeconds(seconds int) int {
	if seconds <= 0 {
		return vmConsoleDefaultTimeout
	}
	if seconds > vmConsoleMaxTimeoutSecs {
		return vmConsoleMaxTimeoutSecs
	}
	return seconds
}

// ── 固定 argv 常量表 ──
// 以下函数只做字符串拼接，所有可变部分（host_node_id/vm_id/capture_id）均已通过
// 严格字符校验，不可能携带 shell 控制字符；拼接结果再经 vmConsoleShellQuote
// 安全引用后交给远端 shell，双层防御确保 argv 逐 token 固定。

// vmConsoleMonitorPath 返回 vtpsh Monitor URI：/nodes/<host_node_id>/qemu/<vm_id>/monitor
func vmConsoleMonitorPath(hostNodeID, vmID string) string {
	return "/nodes/" + hostNodeID + "/qemu/" + vmID + "/monitor"
}

// vmConsoleCapturePath 返回截图临时文件路径：/sf/data/local/hci-diagnosis/<capture_id>.ppm
func vmConsoleCapturePath(captureID string) string {
	return vmConsoleCaptureDir + "/" + captureID + ".ppm"
}

// vmConsoleMonitorArgv 构造 vtpsh Monitor 固定操作的 argv。
func vmConsoleMonitorArgv(hostNodeID, vmID, monitorCommand string) []string {
	return []string{vmConsoleVtpshBinary, "create", vmConsoleMonitorPath(hostNodeID, vmID), "--command", monitorCommand}
}

// vmConsoleScreendumpArgv 基线截图 argv：
// vtpsh create /nodes/<host>/qemu/<vmid>/monitor --command "screendump /sf/data/local/hci-diagnosis/<capture-id>.ppm"
func vmConsoleScreendumpArgv(hostNodeID, vmID, captureID string) []string {
	return vmConsoleMonitorArgv(hostNodeID, vmID, "screendump "+vmConsoleCapturePath(captureID))
}

// vmConsoleWakeArgv 固定唤醒 argv：同 Monitor 路径 --command "sendkey down"。
// 唤醒是受控 Guest 交互（controlled_interaction），不是只读操作。
func vmConsoleWakeArgv(hostNodeID, vmID string) []string {
	return vmConsoleMonitorArgv(hostNodeID, vmID, "sendkey down")
}

// vmConsoleProbeArgv 文件探测 argv：test -f <path>
func vmConsoleProbeArgv(captureID string) []string {
	return []string{"test", "-f", vmConsoleCapturePath(captureID)}
}

// vmConsoleReadArgv 读取 argv：base64 -w0 <path>（单行输出，Go 本地解码）
func vmConsoleReadArgv(captureID string) []string {
	return []string{"base64", "-w0", vmConsoleCapturePath(captureID)}
}

// vmConsoleCleanupArgv 清理 argv：rm -f <path>（无条件执行）
func vmConsoleCleanupArgv(captureID string) []string {
	return []string{"rm", "-f", vmConsoleCapturePath(captureID)}
}

// vmConsoleShellQuote 对单个 argv token 做安全单引号引用。
// 输入已通过字符校验；此处仍按 POSIX 规则引用，防止任何未来放宽校验时的注入回归。
//
// S1 收口：唯一实现在 remote.ShellQuote。旧实现对"安全字符"省略引号，
// 与加引号在 shell 里行为等价（base64 与 'base64' 是同一个词），但两套
// 转义意味着两个注入面各自演化。统一为总是加引号后，本函数单测的
// 断言随之改为带引号形态——这是调用方适配，不是安全语义变更。
func vmConsoleShellQuote(arg string) string {
	return remote.ShellQuote(arg)
}

// vmConsoleCommandFromArgv 把固定 argv 拼接为交给远端 shell 的命令字符串。
func vmConsoleCommandFromArgv(argv []string) string {
	quoted := make([]string, len(argv))
	for i, arg := range argv {
		quoted[i] = vmConsoleShellQuote(arg)
	}
	return strings.Join(quoted, " ")
}

// ── 隔离执行 runner（复用 ssh_exec_process 的隔离连接路径，不走 PTY marker 通道）──

// vmConsoleRunner 在独立 SSH 连接上顺序执行固定操作子步骤。
// 独立连接是硬超时的必要条件（与 execCommandIsolated 同理）；每个子步骤使用
// 独立 Session，stdout/stderr 有界捕获，不向 WebSocket 流式转发大输出。
type vmConsoleRunner struct {
	session *sshx.SSHSession
	req     vmConsoleRequest
	client  *ssh.Client
}

// startVMConsoleRunner 使用会话既有连接参数（含模拟认证上下文）建立隔离连接。
func startVMConsoleRunner(s *sshx.SSHSession, req vmConsoleRequest) (*vmConsoleRunner, error) {
	client, err := ssh.Dial("tcp", s.Address, s.ClientConfig)
	if err != nil {
		return nil, err
	}
	return &vmConsoleRunner{session: s, req: req, client: client}, nil
}

func (r *vmConsoleRunner) close() { _ = r.client.Close() }

// run 执行一条固定命令并返回有界捕获的输出与退出码。
// truncated 表示 stdout 捕获是否因超过 maxStdout 被截断（读取步骤据此判定超限）。
// 超时通过 ctx 强制：deadline 触发时关闭连接确定性中止远端命令。
func (r *vmConsoleRunner) run(ctx context.Context, argv []string, maxStdout int) (stdout []byte, stderrText string, exitCode int, truncated bool, err error) {
	// 模拟会话透传执行上下文（与 execCommandIsolated 的 Setenv 约定一致）。
	traceparent := logpush.TraceparentFromContext(ctx)
	// 新建 Session 前先检查 ctx，避免无谓拨号。
	if ctxErr := ctx.Err(); ctxErr != nil {
		return nil, "", -1, false, ctxErr
	}
	session, err := r.client.NewSession()
	if err != nil {
		return nil, "", -1, false, err
	}
	defer session.Close()
	if r.session.Simulation {
		for name, value := range map[string]string{
			"TRACEPARENT": traceparent,
			"TRACESTATE":  r.req.Tracestate,
			"HTP_EXEC_ID": r.req.ExecID,
			"HTP_NODE_IP": r.req.NodeIP,
		} {
			if strings.TrimSpace(value) == "" {
				continue
			}
			// 模拟运行时拒绝执行上下文时 fail-closed。
			if setenvErr := session.Setenv(name, value); setenvErr != nil {
				return nil, "", -1, false, fmt.Errorf("hci-sim 拒绝执行上下文 %s: %w", name, setenvErr)
			}
		}
	}
	stdoutPipe, err := session.StdoutPipe()
	if err != nil {
		return nil, "", -1, false, err
	}
	stderrPipe, err := session.StderrPipe()
	if err != nil {
		return nil, "", -1, false, err
	}
	command := vmConsoleCommandFromArgv(argv)
	if err := session.Start(command); err != nil {
		return nil, "", -1, false, err
	}

	stdoutCapture := sshx.NewBoundedCapture(maxStdout)
	stderrCapture := sshx.NewBoundedCapture(vmConsoleMaxStderrBytes)
	var readers sync.WaitGroup
	readers.Add(2)
	go func() {
		defer readers.Done()
		buffer := make([]byte, 32*1024)
		for {
			n, readErr := stdoutPipe.Read(buffer)
			if n > 0 {
				stdoutCapture.Write(buffer[:n])
			}
			if readErr != nil {
				return
			}
		}
	}()
	go func() {
		defer readers.Done()
		buffer := make([]byte, 8*1024)
		for {
			n, readErr := stderrPipe.Read(buffer)
			if n > 0 {
				stderrCapture.Write(buffer[:n])
			}
			if readErr != nil {
				return
			}
		}
	}()

	waitCh := make(chan error, 1)
	go func() { waitCh <- session.Wait() }()
	var waitErr error
	select {
	case waitErr = <-waitCh:
	case <-ctx.Done():
		// 关闭隔离连接确定性中止命令（同 execCommandIsolated 超时语义）。
		_ = session.Close()
		_ = r.client.Close()
		waitErr = <-waitCh
	}
	readers.Wait()
	if ctxErr := ctx.Err(); ctxErr != nil {
		return nil, stderrCapture.String(), -1, stdoutCapture.Truncated, ctxErr
	}
	exitCode = 0
	if waitErr != nil {
		if exitErr, ok := waitErr.(*ssh.ExitError); ok {
			exitCode = exitErr.ExitStatus()
		} else {
			exitCode = -1
			return stdoutCapture.Bytes(), stderrCapture.String(), exitCode, stdoutCapture.Truncated, waitErr
		}
	}
	return stdoutCapture.Bytes(), stderrCapture.String(), exitCode, stdoutCapture.Truncated, nil
}

// ── 主流程与结果回传 ──

// vmConsoleResultMessage 组装 vm_console_result 元数据消息（WS 只回元数据）。
func vmConsoleResultMessage(req vmConsoleRequest, exitCode int, traceID, traceparent string, durationMS int64) models.OutMessage {
	return models.OutMessage{
		Type: "vm_console_result", CaseID: req.CaseID, ExecID: req.ExecID,
		CaptureID: req.CaptureID, Operation: req.Operation,
		ExitCode: exitCode, TraceID: traceID, Traceparent: traceparent,
		DurationMS: durationMS,
	}
}

// runVMConsoleOp 执行一次固定操作（capture_baseline 或 wake_down_key）并回传元数据。
// 必须在 goroutine 中调用：文件探测轮询与 HTTP 直传可能阻塞数十秒。
// send 为结果回传函数（生产路径封装 models.SendMsg(ws, ...)，测试可注入记录器）。
func runVMConsoleOp(s *sshx.SSHSession, send func(models.OutMessage), req vmConsoleRequest) {
	startTime := time.Now()
	baseCtx := logpush.ContextFromMessage(models.InMessage{Traceparent: req.Traceparent, Tracestate: req.Tracestate})
	ctx, cancel := context.WithTimeout(baseCtx, time.Duration(req.TimeoutSeconds)*time.Second)
	defer cancel()
	ctx, span := otel.Tracer("terminal_bridge").Start(ctx, "terminal_bridge.vm_console."+req.Operation,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("exec.id", req.ExecID),
			attribute.String("case.id", req.CaseID),
			attribute.String("conversation.id", req.ConversationID),
			attribute.String("server.Address", req.NodeIP),
			attribute.String("vm_console.operation", req.Operation),
			attribute.String("vm_console.capture_id", req.CaptureID),
			attribute.String("vm_console.host_node_id", req.HostNodeID),
			attribute.String("vm_console.vm_id", req.VMID),
		),
	)
	defer span.End()
	traceID := span.SpanContext().TraceID().String()

	logReq := models.ExecRequestContext{
		Context: ctx, CaseID: req.CaseID, ConversationID: req.ConversationID,
		ExecID: req.ExecID, TraceID: req.TraceID, Traceparent: req.Traceparent,
		Tracestate: req.Tracestate, NodeIP: req.NodeIP, CustomUI: req.CustomUI,
		// 命令完全由常量表决定，日志直接记录 argv 摘要（无敏感信息）。
		CommandRedacted: "vm_console:" + req.Operation,
	}

	sendResult := func(result models.OutMessage) {
		result.Type = "vm_console_result"
		result.CaseID = req.CaseID
		result.ExecID = req.ExecID
		result.CaptureID = req.CaptureID
		result.Operation = req.Operation
		result.TraceID = traceID
		result.Traceparent = logpush.TraceparentFromContext(ctx)
		result.DurationMS = time.Since(startTime).Milliseconds()
		send(result)
	}
	failResult := func(errorType string, exitCode int, timedOut bool) {
		if errorType != "" {
			atomic.AddUint64(&promMetrics.VmConsoleOpErrors, 1)
			span.SetStatus(codes.Error, errorType)
		}
		result := vmConsoleResultMessage(req, exitCode, traceID, logpush.TraceparentFromContext(ctx), time.Since(startTime).Milliseconds())
		result.ErrorType = errorType
		result.TimedOut = timedOut
		send(result)
	}

	logpush.BlogContext(ctx, "INFO", "vm_console.start", "开始执行虚拟机控制台固定操作", logReq, map[string]any{
		"operation": req.Operation, "capture_id": req.CaptureID,
		"host_node_id": req.HostNodeID, "vm_id": req.VMID, "timeout_seconds": req.TimeoutSeconds,
	})

	runner, err := startVMConsoleRunner(s, req)
	if err != nil {
		span.RecordError(err)
		logpush.BlogContext(ctx, "ERROR", "vm_console.error", "创建隔离 SSH 连接失败", logReq, map[string]any{"error": err.Error(), "error_type": vmConsoleErrSessionCreate})
		failResult(vmConsoleErrSessionCreate, -1, false)
		return
	}
	defer runner.close()

	if req.Operation == vmConsoleOpWakeDownKey {
		runVMConsoleWake(s, ctx, runner, req, logReq, span, failResult, func(result models.OutMessage) { sendResult(result) })
		return
	}
	runVMConsoleCaptureBaseline(s, ctx, runner, req, logReq, span, func(result models.OutMessage) { sendResult(result) })
}

// runVMConsoleWake 执行固定唤醒操作（sendkey down），不做任何上传。
func runVMConsoleWake(s *sshx.SSHSession, ctx context.Context, runner *vmConsoleRunner, req vmConsoleRequest, logReq models.ExecRequestContext,
	span trace.Span, failResult func(string, int, bool), sendResult func(models.OutMessage),) {
	_, stderrText, exitCode, _, err := runner.run(ctx, vmConsoleWakeArgv(req.HostNodeID, req.VMID), vmConsoleMaxStderrBytes)
	timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
	if err != nil {
		span.RecordError(err)
		logpush.BlogContext(ctx, "ERROR", "vm_console.wake_failed", "固定唤醒操作失败", logReq, map[string]any{
			"error": err.Error(), "error_type": vmConsoleErrWakeFailed, "timed_out": timedOut,
		})
		if timedOut {
			failResult(vmConsoleErrTimeout, -1, true)
			return
		}
		failResult(vmConsoleErrWakeFailed, -1, false)
		return
	}
	errorType := ""
	if exitCode != 0 {
		errorType = vmConsoleErrWakeFailed
		logpush.BlogContext(ctx, "ERROR", "vm_console.wake_failed", "固定唤醒操作退出码非零", logReq, map[string]any{
			"exit_code": exitCode, "stderr_len": len(stderrText),
		})
	} else {
		logpush.BlogContext(ctx, "INFO", "vm_console.wake_done", "固定唤醒操作完成", logReq, map[string]any{"exit_code": 0})
	}
	result := vmConsoleResultMessage(req, exitCode, "", "", 0)
	result.UploadStatus = vmConsoleUploadNotApplicable
	result.ErrorType = errorType
	if errorType != "" {
		atomic.AddUint64(&promMetrics.VmConsoleOpErrors, 1)
		span.SetStatus(codes.Error, errorType)
	}
	sendResult(result)
}

// runVMConsoleCaptureBaseline 执行基线截图全流程：
// screendump → 有界轮询探测 → base64 读取 → 本地解码 + SHA-256 → HTTP 直传 → 无条件清理。
// cleanup 在发送结果前执行，保证 cleanup_failed 标记可进入最终 error_type。
func runVMConsoleCaptureBaseline(s *sshx.SSHSession, ctx context.Context, runner *vmConsoleRunner, req vmConsoleRequest, logReq models.ExecRequestContext,
	span trace.Span, sendResult func(models.OutMessage),) {
	result, errorType, timedOut := captureBaselineSteps(s, ctx, runner, req, logReq, span)

	// cleanup 无条件执行：无论成功失败都尝试删除宿主机临时文件，失败仅附加标记不阻断结果。
	if cleanupErr := vmConsoleCleanup(s, ctx, runner, req, logReq); cleanupErr != nil {
		if errorType == "" {
			errorType = vmConsoleErrCleanupFailed
		} else {
			errorType += ";" + vmConsoleErrCleanupFailed
		}
	}

	result.ErrorType = errorType
	result.TimedOut = timedOut
	if errorType != "" {
		atomic.AddUint64(&promMetrics.VmConsoleOpErrors, 1)
		span.SetStatus(codes.Error, errorType)
	}
	logpush.BlogContext(ctx, "INFO", "vm_console.capture_done", "基线截图流程结束", logReq, map[string]any{
		"exit_code": result.ExitCode, "upload_status": result.UploadStatus,
		"sha256": result.SHA256, "size_bytes": result.SizeBytes,
		"error_type": errorType, "timed_out": timedOut,
	})
	sendResult(result)
}

// captureBaselineSteps 顺序执行基线截图各子步骤，返回 (部分结果, error_type, timed_out)。
func captureBaselineSteps(s *sshx.SSHSession, ctx context.Context, runner *vmConsoleRunner, req vmConsoleRequest, logReq models.ExecRequestContext, span trace.Span,) (models.OutMessage, string, bool) {
	result := vmConsoleResultMessage(req, 0, "", "", 0)

	// 步骤 1：固定 screendump 命令。
	_, stderrText, exitCode, _, err := runner.run(ctx, vmConsoleScreendumpArgv(req.HostNodeID, req.VMID, req.CaptureID), vmConsoleMaxStderrBytes)
	if err != nil {
		span.RecordError(err)
		timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
		logpush.BlogContext(ctx, "ERROR", "vm_console.capture_failed", "screendump 命令执行失败", logReq, map[string]any{
			"error": err.Error(), "timed_out": timedOut,
		})
		if timedOut {
			result.ExitCode = -1
			return result, vmConsoleErrTimeout, true
		}
		result.ExitCode = -1
		return result, vmConsoleErrCaptureFailed, false
	}
	if exitCode != 0 {
		logpush.BlogContext(ctx, "ERROR", "vm_console.capture_failed", "screendump 命令退出码非零", logReq, map[string]any{
			"exit_code": exitCode, "stderr_len": len(stderrText),
		})
		result.ExitCode = exitCode
		return result, vmConsoleErrCaptureFailed, false
	}

	// 步骤 2：固定有界轮询探测截图文件（最多 10 次、每次间隔 1s，总等待受操作预算约束）。
	found := false
	for attempt := 1; attempt <= vmConsoleProbeMaxAttempts; attempt++ {
		if ctx.Err() != nil {
			break
		}
		_, _, probeCode, _, probeErr := runner.run(ctx, vmConsoleProbeArgv(req.CaptureID), vmConsoleMaxStderrBytes)
		if probeErr == nil && probeCode == 0 {
			found = true
			break
		}
		if attempt == vmConsoleProbeMaxAttempts {
			break
		}
		select {
		case <-time.After(vmConsoleProbeInterval):
		case <-ctx.Done():
		}
	}
	if !found {
		timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
		logpush.BlogContext(ctx, "ERROR", "vm_console.capture_file_missing", "有界轮询后截图文件仍不存在", logReq, map[string]any{
			"probe_attempts": vmConsoleProbeMaxAttempts, "timed_out": timedOut,
		})
		result.ExitCode = -1
		if timedOut {
			return result, vmConsoleErrTimeout, true
		}
		return result, vmConsoleErrCaptureMissing, false
	}

	// 步骤 3：固定 base64 读取（BoundedCapture 上限 16MiB，不向 WS 转发）。
	stdoutBytes, readStderr, readCode, readTruncated, readErr := runner.run(ctx, vmConsoleReadArgv(req.CaptureID), vmConsoleMaxBase64Bytes)
	if readErr != nil {
		span.RecordError(readErr)
		timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
		logpush.BlogContext(ctx, "ERROR", "vm_console.read_failed", "base64 读取命令执行失败", logReq, map[string]any{
			"error": readErr.Error(), "timed_out": timedOut,
		})
		result.ExitCode = -1
		if timedOut {
			return result, vmConsoleErrTimeout, true
		}
		return result, vmConsoleErrReadFailed, false
	}
	if readCode != 0 {
		logpush.BlogContext(ctx, "ERROR", "vm_console.read_failed", "base64 读取命令退出码非零", logReq, map[string]any{
			"exit_code": readCode, "stderr_len": len(readStderr),
		})
		result.ExitCode = readCode
		return result, vmConsoleErrReadFailed, false
	}

	// 步骤 4：Go 本地解码并计算 SHA-256；超限与畸形数据 fail-closed。
	encoded := bytes.TrimSpace(stdoutBytes)
	if readTruncated {
		// 捕获被截断说明原始文件必然超过安全上限，绝不把不完整数据传给制品服务。
		logpush.BlogContext(ctx, "ERROR", "vm_console.capture_too_large", "base64 输出超过有界捕获上限", logReq, map[string]any{
			"limit_bytes": vmConsoleMaxBase64Bytes,
		})
		result.ExitCode = -1
		return result, vmConsoleErrTooLarge, false
	}
	decoded, decodeErr := base64Decode(encoded)
	if decodeErr != nil {
		logpush.BlogContext(ctx, "ERROR", "vm_console.capture_decode_failed", "base64 解码失败", logReq, map[string]any{
			"error": decodeErr.Error(), "encoded_len": len(encoded),
		})
		result.ExitCode = -1
		return result, vmConsoleErrDecodeFailed, false
	}
	if len(decoded) > vmConsoleMaxCaptureBytes {
		logpush.BlogContext(ctx, "ERROR", "vm_console.capture_too_large", "解码后截图超过大小上限", logReq, map[string]any{
			"size_bytes": len(decoded), "limit_bytes": vmConsoleMaxCaptureBytes,
		})
		result.ExitCode = -1
		return result, vmConsoleErrTooLarge, false
	}
	result.SHA256 = fmt.Sprintf("%x", sha256.Sum256(decoded))
	result.SizeBytes = int64(len(decoded))

	// 步骤 5：PPM 直传平台制品服务（独立超时 ~60s，不占用操作预算）。
	// PLATFORM_ARTIFACT_URL 未配置 → fail-closed artifact_upload_disabled，不降级 base64 over WS。
	uploadStatus, uploadErr := uploadVMConsolePPM(context.Background(), req.CaptureID, req.CaseID, decoded, result.SHA256)
	if uploadErr != nil {
		logpush.BlogContext(ctx, "ERROR", "vm_console.upload_failed", "PPM 直传失败", logReq, map[string]any{
			"error": uploadErr.Error(), "sha256": result.SHA256, "size_bytes": result.SizeBytes,
			"capture_id": req.CaptureID, "upload_status": uploadStatus,
		})
	} else {
		logpush.BlogContext(ctx, "INFO", "vm_console.upload_done", "PPM 直传完成", logReq, map[string]any{
			"upload_status": uploadStatus, "sha256": result.SHA256, "size_bytes": result.SizeBytes,
		})
	}
	result.UploadStatus = uploadStatus
	switch uploadStatus {
	case vmConsoleUploadFailed:
		return result, vmConsoleErrUploadFailed, false
	case vmConsoleUploadDisabled:
		// fail-closed：未配置制品服务时截图不可用，同时落入 error_type 便于编排识别。
		return result, vmConsoleUploadDisabled, false
	}
	return result, "", false
}

// vmConsoleCleanup 无条件删除宿主机临时截图文件；失败返回错误供上层附加标记。
func vmConsoleCleanup(s *sshx.SSHSession, ctx context.Context, runner *vmConsoleRunner, req vmConsoleRequest, logReq models.ExecRequestContext) error {
	// 清理使用独立短超时，避免占用已耗尽的操作预算；ctx 已取消时使用后台上下文。
	cleanupCtx := ctx
	if cleanupCtx.Err() != nil {
		var cleanupCancel context.CancelFunc
		cleanupCtx, cleanupCancel = context.WithTimeout(context.Background(), vmConsoleCleanupTimeout)
		defer cleanupCancel()
	}
	_, stderrText, exitCode, _, err := runner.run(cleanupCtx, vmConsoleCleanupArgv(req.CaptureID), vmConsoleMaxStderrBytes)
	if err != nil {
		logpush.BlogContext(ctx, "WARN", "vm_console.cleanup_failed", "清理临时截图文件失败", logReq, map[string]any{"error": err.Error()})
		return err
	}
	if exitCode != 0 {
		logpush.BlogContext(ctx, "WARN", "vm_console.cleanup_failed", "清理临时截图文件退出码非零", logReq, map[string]any{
			"exit_code": exitCode, "stderr_len": len(stderrText),
		})
		return fmt.Errorf("rm -f 退出码 %d", exitCode)
	}
	logpush.BlogContext(ctx, "INFO", "vm_console.cleanup_done", "临时截图文件已清理", logReq, nil)
	return nil
}

// base64Decode 使用标准 base64 解码（容忍尾部换行）。
func base64Decode(encoded []byte) ([]byte, error) {
	return base64.StdEncoding.DecodeString(string(encoded))
}

// uploadVMConsolePPM 将原始 PPM 直传平台制品服务（conversation-service 基址）。
// 返回 (upload_status, error)：
//   - PLATFORM_ARTIFACT_URL 未配置 → (artifact_upload_disabled, nil)，fail-closed 不降级；
//   - 上传成功（2xx）→ (uploaded, nil)；
//   - 其他失败 → (upload_failed, err)。
func uploadVMConsolePPM(parent context.Context, captureID, caseID string, data []byte, sha256Hex string) (string, error) {
	base := strings.TrimRight(strings.TrimSpace(os.Getenv("PLATFORM_ARTIFACT_URL")), "/")
	if base == "" {
		return vmConsoleUploadDisabled, nil
	}
	query := url.Values{}
	query.Set("kind", "ppm")
	query.Set("case_id", caseID)
	query.Set("mode", "online")
	uploadURL := fmt.Sprintf("%s/internal/vm-console/artifacts/%s?%s", base, captureID, query.Encode())
	ctx, cancel := context.WithTimeout(parent, vmConsoleUploadTimeout)
	defer cancel()
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, uploadURL, bytes.NewReader(data))
	if err != nil {
		return vmConsoleUploadFailed, err
	}
	httpReq.Header.Set("Content-Type", "application/octet-stream")
	httpReq.Header.Set("X-Capture-Sha256", sha256Hex)
	if token := strings.TrimSpace(os.Getenv("PLATFORM_INTERNAL_API_TOKEN")); token != "" {
		httpReq.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		return vmConsoleUploadFailed, err
	}
	defer response.Body.Close()
	// 有界排空响应体，便于连接复用并避免悬挂。
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64*1024))
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		return vmConsoleUploadOK, nil
	}
	return vmConsoleUploadFailed, fmt.Errorf("制品服务返回 HTTP %d", response.StatusCode)
}

// ── acli 自动检查与更新（acli_sync）──────────────────────────────────────────
