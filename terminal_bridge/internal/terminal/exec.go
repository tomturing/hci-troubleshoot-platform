package terminal

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"regexp"
	"strings"
	"sync"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/models"
	bruntime "terminal_bridge/internal/shared/runtime"
	"terminal_bridge/internal/shared/telemetry"
	"terminal_bridge/internal/sshx"
	"time"
	"unicode/utf8"

	"golang.org/x/crypto/ssh"
	"golang.org/x/net/websocket"
	"golang.org/x/text/encoding/simplifiedchinese"
	"golang.org/x/text/transform"
)

func ExecCommandIsolated(s *sshx.SSHSession, ws *websocket.Conn, req models.ExecRequestContext, requestedTimeout time.Duration, outputFilters []models.OutputFilter) {
	// 模拟属性固定于连接创建时验证的认证上下文，host/DNS 名不会改变其语义。
	sim, execMode, simBackend := s.Simulation, s.ExecutionMode, s.SimulationBackend

	// PTY 在单一入口判定：保证日志、输出语义与真实终端行为一致，避免各调用点各自为政。
	req.Pty = shouldUsePty(req.Command, req.Pty)
	ptyMode := "pipe"
	if req.Pty {
		ptyMode = "pty"
	}

	timeout := requestedTimeout
	if timeout <= 0 {
		timeout = CommandTimeout(bruntime.EnvIntOrDefault("HCI_BRIDGE_EXEC_TIMEOUT_SECONDS", 120))
	}
	if err := ValidateOutputFilters(outputFilters); err != nil {
		errType := "invalid_output_filter"
		evidence.EmitContext(req.Context, "ERROR", "exec.error", "输出筛选参数无效", req, map[string]any{
			"error": err.Error(), "error_type": errType,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID,
			Stderr: err.Error(), ExitCode: -1, TraceID: req.TraceID,
			ErrorType: errType,
		})
		return
	}
	maxOutputBytes := bruntime.EnvIntOrDefault("HCI_BRIDGE_EXEC_MAX_OUTPUT_BYTES", 4*1024*1024)
	ctx, cancel := context.WithTimeout(req.Context, timeout)
	defer cancel()
	ctx, span := telemetry.StartClient(ctx, "terminal_bridge.ssh.exec",
		telemetry.String("exec.id", req.ExecID),
		telemetry.String("case.id", req.CaseID),
		telemetry.String("conversation.id", req.ConversationID),
		telemetry.String("server.Address", req.NodeIP),
		telemetry.String("hci.container", req.Container),
		telemetry.String("command.sha256", req.CommandSHA256),
	)
	defer span.End()
	startTime := time.Now()
	traceparent := evidence.TraceparentFromContext(ctx)
	traceID := span.TraceID()
	artifactID := evidence.DeterministicArtifactID(req.ExecID)

	evidence.EmitContext(ctx, "INFO", "exec.start", "开始执行命令", req, map[string]any{
		"command_redacted": req.CommandRedacted,
		"command_sha256":   req.CommandSHA256,
		"command_len":      len(req.Command),
	})

	isolatedClient, err := ssh.Dial("tcp", s.Address, s.ClientConfig)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(telemetry.StatusError, "session_creation_failed")
		MetricsExecErrors(1)
		evidence.EmitContext(ctx, "ERROR", "exec.error", "创建隔离 SSH 连接失败", req, map[string]any{"error": err.Error(), "error_type": "session_creation_failed"})
		models.SendMsg(ws, models.OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "创建隔离 SSH 连接失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "session_creation_failed"})
		return
	}
	defer isolatedClient.Close()

	session, err := isolatedClient.NewSession()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(telemetry.StatusError, "session_creation_failed")
		MetricsExecErrors(1)
		evidence.EmitContext(ctx, "ERROR", "exec.error", "创建隔离 SSH 会话失败", req, map[string]any{"error": err.Error(), "error_type": "session_creation_failed"})
		models.SendMsg(ws, models.OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "创建隔离 SSH 会话失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "session_creation_failed"})
		return
	}
	defer session.Close()
	// PTY 必须在 Start 之前申请。申请失败不允许降级：acli 插件在非 TTY 下会以
	// "input is not a terminal" 失败但退出码仍为 0，降级等于把失败伪装成成功。
	if req.Pty {
		if err := session.RequestPty("xterm-256color", PtyTerminalHeight, PtyTerminalWidth, PtyTerminalModes()); err != nil {
			span.RecordError(err)
			span.SetStatus(telemetry.StatusError, "pty_request_failed")
			MetricsExecErrors(1)
			evidence.EmitContext(ctx, "ERROR", "exec.error", "申请伪终端失败", req, map[string]any{"error": err.Error(), "error_type": "pty_request_failed"})
			models.SendMsg(ws, models.OutMessage{
				Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID,
				Stderr: "申请伪终端失败（该命令必须使用 PTY）", ExitCode: -1,
				TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID,
				ErrorType: "pty_request_failed", PtyMode: ptyMode,
			})
			return
		}
	}
	if s.Simulation {
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
				span.SetStatus(telemetry.StatusError, "simulation_env_rejected")
				MetricsExecErrors(1)
				evidence.EmitContext(ctx, "ERROR", "exec.error", "hci-sim 拒绝执行上下文", req, map[string]any{"error": err.Error(), "error_type": "simulation_env_rejected", "env_name": name})
				models.SendMsg(ws, models.OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "hci-sim 拒绝执行上下文", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "simulation_env_rejected"})
				return
			}
		}
	}

	stdoutPipe, err := session.StdoutPipe()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(telemetry.StatusError, "stdout_pipe_failed")
		MetricsExecErrors(1)
		evidence.EmitContext(ctx, "ERROR", "exec.error", "获取 StdoutPipe 失败", req, map[string]any{"error": err.Error(), "error_type": "stdout_pipe_failed"})
		models.SendMsg(ws, models.OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "获取标准输出失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "stdout_pipe_failed"})
		return
	}
	// PTY 模式下服务端把 stderr 合并进同一终端流，StderrPipe 不会产生独立数据。
	// 此时不创建 stderr 读取，并在结果中显式声明 stderr_pty_merged，避免下游把
	// "stderr 为空"误解为"没有错误输出"。
	var stderrPipe io.Reader
	if !req.Pty {
		stderrPipe, err = session.StderrPipe()
		if err != nil {
			span.RecordError(err)
			span.SetStatus(telemetry.StatusError, "stderr_pipe_failed")
			MetricsExecErrors(1)
			evidence.EmitContext(ctx, "ERROR", "exec.error", "获取 StderrPipe 失败", req, map[string]any{"error": err.Error(), "error_type": "stderr_pipe_failed"})
			models.SendMsg(ws, models.OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "获取标准错误失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "stderr_pipe_failed", PtyMode: ptyMode})
			return
		}
	}

	if err := session.Start(req.Command); err != nil {
		span.RecordError(err)
		span.SetStatus(telemetry.StatusError, "command_start_failed")
		MetricsExecErrors(1)
		evidence.EmitContext(ctx, "ERROR", "exec.error", "启动命令失败", req, map[string]any{"error": err.Error(), "error_type": "command_start_failed"})
		models.SendMsg(ws, models.OutMessage{Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: "启动命令失败", ExitCode: -1, TraceID: traceID, Traceparent: traceparent, ArtifactID: artifactID, ErrorType: "command_start_failed"})
		return
	}

	stdoutCapture := sshx.NewBoundedCapture(maxOutputBytes)
	stderrCapture := sshx.NewBoundedCapture(maxOutputBytes)
	stdoutFiltered := len(FiltersForSource(outputFilters, "stdout")) > 0
	stderrFiltered := len(FiltersForSource(outputFilters, "stderr")) > 0
	stdoutStats := sshx.NewStreamStats(stdoutFiltered)
	stderrStats := sshx.NewStreamStats(stderrFiltered)
	var readers sync.WaitGroup
	stdoutReader := io.Reader(stdoutPipe)
	if req.Pty {
		// PTY 输出常为 CRLF，统一在读取层归一化，保证 capture 哈希与最终 stdout 一致。
		stdoutReader = &crlfNormalizingReader{src: stdoutPipe}
	}
	readers.Add(1)
	stdoutEmit := func(chunk string) {
		models.SendMsg(ws, models.OutMessage{Type: "exec_stdout", CaseID: req.CaseID, ExecID: req.ExecID, Stdout: chunk, TraceID: traceID})
	}
	stderrEmit := func(chunk string) {
		models.SendMsg(ws, models.OutMessage{Type: "exec_stderr", CaseID: req.CaseID, ExecID: req.ExecID, Stderr: chunk, TraceID: traceID})
	}
	if stdoutFiltered {
		go DrainExecPipeFiltered(stdoutReader, stdoutCapture, stdoutStats, stdoutEmit, &readers, "stdout", outputFilters)
	} else {
		go DrainExecPipe(stdoutReader, stdoutCapture, stdoutStats, stdoutEmit, &readers)
	}
	if !req.Pty {
		readers.Add(1)
		if stderrFiltered {
			go DrainExecPipeFiltered(stderrPipe, stderrCapture, stderrStats, stderrEmit, &readers, "stderr", outputFilters)
		} else {
			go DrainExecPipe(stderrPipe, stderrCapture, stderrStats, stderrEmit, &readers)
		}
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
	stdoutStats.Finish()
	stderrStats.Finish()

	exitCode := 0
	errorType := ""
	if timedOut {
		exitCode = -1
		errorType = "timeout"
		// 超时显式成事件：此前只体现在 exec.done 的 timed_out 字段里，排障时无法独立检索。
		evidence.EmitContext(ctx, "ERROR", "exec.timeout", "命令执行超时", req, map[string]any{
			"exec_id": req.ExecID, "error_type": "timeout",
			"timeout_seconds": timeout.Seconds(), "stdout_len": stdoutCapture.Total,
		})
	} else if waitErr != nil {
		if exitErr, ok := waitErr.(*ssh.ExitError); ok {
			exitCode = exitErr.ExitStatus()
			// 非零退出：按 stderr/输出细分语义，禁止一律 nonzero_exit（如 jq 缺 ONIGURUMA→tool_capability_missing）
			if classified := sshx.ClassifyExecFailure(exitCode, stderrCapture.String()+"\n"+stdoutCapture.String()); classified != "nonzero_exit" {
				errorType = classified
			} else {
				errorType = "nonzero_exit"
			}
		} else {
			exitCode = -1
			errorType = "ssh_wait_failed"
		}
	}
	// 修复 E：对端不可达/超时/建会话失败时触发一组廉价辅助探测，为复盘提供可判别依据。
	if timedOut || errorType == "session_creation_failed" || errorType == "ssh_wait_failed" {
		sshx.RunUnreachabilityProbes(ctx, s, req, artifactID)
	}
	if exitCode != 0 {
		MetricsExecErrors(1)
		span.SetStatus(telemetry.StatusError, errorType)
	}

	duration := time.Since(startTime)
	level := "INFO"
	if exitCode != 0 {
		level = "ERROR"
	}
	span.SetAttributes(
		telemetry.Bool("exec.pty", req.Pty),
		telemetry.Int("process.exit.code", exitCode),
		telemetry.Int64("exec.duration_ms", duration.Milliseconds()),
		telemetry.Int64("stdout.bytes", stdoutCapture.Total),
		telemetry.Int64("stderr.bytes", stderrCapture.Total),
		telemetry.Int64("stdout.raw_bytes", stdoutStats.RawBytes),
		telemetry.Int64("stderr.raw_bytes", stderrStats.RawBytes),
		telemetry.Int64("stdout.scanned_lines", stdoutStats.ScannedLines),
		telemetry.Int64("stderr.scanned_lines", stderrStats.ScannedLines),
		telemetry.Int64("stdout.kept_lines", stdoutStats.KeptLines),
		telemetry.Int64("stderr.kept_lines", stderrStats.KeptLines),
		telemetry.Bool("stdout.filter_applied", stdoutStats.Filtered),
		telemetry.Bool("stderr.filter_applied", stderrStats.Filtered),
		telemetry.Bool("stdout.Truncated", stdoutCapture.Truncated),
		telemetry.Bool("stderr.Truncated", stderrCapture.Truncated),
		telemetry.String("artifact.id", artifactID),
	)
	evidence.EmitContext(ctx, level, "exec.done", "命令执行完成", req, map[string]any{
		"artifact_id":            artifactID,
		"exit_code":              exitCode,
		"success":                exitCode == 0,
		"error_type":             errorType,
		"duration_ms":            duration.Milliseconds(),
		"stdout_len":             stdoutCapture.Total,
		"stderr_len":             stderrCapture.Total,
		"stdout_sha256":          stdoutCapture.SHA256(),
		"stderr_sha256":          stderrCapture.SHA256(),
		"stdout_raw_bytes":       stdoutStats.RawBytes,
		"stderr_raw_bytes":       stderrStats.RawBytes,
		"stdout_raw_sha256":      stdoutStats.RawSHA256(),
		"stderr_raw_sha256":      stderrStats.RawSHA256(),
		"stdout_scanned_lines":   stdoutStats.ScannedLines,
		"stderr_scanned_lines":   stderrStats.ScannedLines,
		"stdout_kept_lines":      stdoutStats.KeptLines,
		"stderr_kept_lines":      stderrStats.KeptLines,
		"stdout_filter_applied":  stdoutStats.Filtered,
		"stderr_filter_applied":  stderrStats.Filtered,
		"stdout_filtered_bytes":  stdoutCapture.Total,
		"stderr_filtered_bytes":  stderrCapture.Total,
		"stdout_filtered_sha256": stdoutCapture.SHA256(),
		"stderr_filtered_sha256": stderrCapture.SHA256(),
		"stdout_truncated":       stdoutCapture.Truncated,
		"stderr_truncated":       stderrCapture.Truncated,
		"timed_out":              timedOut,
		"pty":                    req.Pty,
		// 失败语义留痕：非零/超时时保留 stderr 预览，避免日志只有 exit_code=-1 而无任何原因
		"stderr_preview": sshx.TruncateString(stderrCapture.String(), 512),
	})

	resultCtx, resultSpan := telemetry.Start(ctx, "terminal_bridge.websocket.result.send",
		telemetry.String("exec.id", req.ExecID),
	)
	finalStdout := stdoutCapture.String()
	if req.Pty {
		// reader 已归一化，此处幂等兜底，防止实现变更后 CRLF 泄漏到上层解析。
		finalStdout = NormalizePtyOutput(finalStdout)
	}
	models.SendMsg(ws, models.OutMessage{
		Type: "exec_result", CaseID: req.CaseID, ExecID: req.ExecID,
		Stdout: finalStdout, Stderr: stderrCapture.String(), ExitCode: exitCode,
		PtyMode: ptyMode, StderrPtyMerged: req.Pty,
		TraceID: traceID, Traceparent: evidence.TraceparentFromContext(resultCtx), ArtifactID: artifactID,
		StdoutBytes: stdoutCapture.Total, StderrBytes: stderrCapture.Total,
		StdoutSHA256: stdoutCapture.SHA256(), StderrSHA256: stderrCapture.SHA256(),
		StdoutTruncated: stdoutCapture.Truncated, StderrTruncated: stderrCapture.Truncated,
		DurationMS: duration.Milliseconds(), TimedOut: timedOut, ErrorType: errorType,
		Simulation: sim, ExecutionMode: execMode, SimulationBackend: simBackend,
	})
	resultSpan.End()
}

func DrainExecPipe(pipe io.Reader, capture *sshx.BoundedCapture, stats *sshx.StreamStats, emit func(string), wg *sync.WaitGroup) {
	defer wg.Done()
	buffer := make([]byte, 4096)
	for {
		n, err := pipe.Read(buffer)
		if n > 0 {
			stats.RecordRaw(buffer[:n])
			if captured := capture.Write(buffer[:n]); len(captured) > 0 {
				emit(string(captured))
			}
		}
		if err != nil {
			return
		}
	}
}

func FiltersForSource(filters []models.OutputFilter, source string) []models.OutputFilter {
	selected := make([]models.OutputFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Source == source && (len(filter.Include) > 0 || len(filter.Exclude) > 0) {
			selected = append(selected, filter)
		}
	}
	return selected
}

func LineMatchesOutputFilter(line string, filter models.OutputFilter) bool {
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

// DrainExecPipeFiltered 在 Bridge 本地逐行筛选，原始大输出不会进入 WebSocket 和浏览器。
func DrainExecPipeFiltered(pipe io.Reader, capture *sshx.BoundedCapture, stats *sshx.StreamStats, emit func(string), wg *sync.WaitGroup, source string, filters []models.OutputFilter) {
	defer wg.Done()
	selected := FiltersForSource(filters, source)
	reader := bufio.NewReader(pipe)
	for {
		line, err := reader.ReadString('\n')
		if line != "" {
			stats.RecordRaw([]byte(line))
			keep := false
			for _, filter := range selected {
				if LineMatchesOutputFilter(line, filter) {
					keep = true
					break
				}
			}
			if keep {
				stats.KeptLines++
				if captured := capture.Write([]byte(line)); len(captured) > 0 {
					emit(string(captured))
				}
			}
		}
		if err != nil {
			return
		}
	}
}

func ValidateOutputFilters(filters []models.OutputFilter) error {
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

func CommandTimeout(seconds int) time.Duration {
	if seconds < 1 || seconds > 300 {
		seconds = 60
	}
	return time.Duration(seconds) * time.Second
}

func On_output_start(s *sshx.SSHSession, ws *websocket.Conn,
	stdout io.ReadCloser,
	caseID string,
	// onExit 带上结束原因：调用方据此区分「远端退出」与「我们主动关闭」，
	// 只有前者才需要记 session.destroyed（主动关闭已由 destroySession 留痕）。
	onExit func(reason string)) {
	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := stdout.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				s.AppendOutput(chunk)

				if listener, exitCode, matched := s.CheckMarkers(chunk); matched {
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

					markerErrType := sshx.ClassifyExecFailure(exitCode, output)
					// ws 可能为 nil：交付后台作业重连节点时没有浏览器连接，
					// 只需保留 exec 结果回环（runCommand 依赖它），不必回发终端。
					if ws != nil {
						models.SendMsg(ws, models.OutMessage{
							Type: "exec_result", CaseID: caseID, ExecID: listener.ExecID,
							Output: output, ExitCode: exitCode, ErrorType: markerErrType,
						})
					}
					s.UnregisterExecListener(listener.ExecID)
					select {
					case listener.ResultChan <- sshx.ExecResult{Output: output, ExitCode: exitCode, ErrorType: markerErrType}:
					default:
					}
				}

				if ws != nil {
					models.SendMsg(ws, models.OutMessage{Type: "ssh_output", CaseID: caseID, Output: chunk})
				}

				// 缺陷 2: 回采 SSH 终端交互日志（用户手动输入/输出的关键信息）
				// 只回采有意义的内容（过滤空行和控制序列）
				if len(chunk) > 0 && chunk != "\r\n" && !strings.Contains(chunk, "\x1b[") {
					evidence.Emit("INFO", "ssh.output", "SSH 终端输出", "", caseID, "", "", map[string]any{
						"output_len":     len(chunk),
						"output_preview": sshx.TruncateString(chunk, 200),
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

		// 区分「谁结束了这条 SSH 连接」：local_close 是我们主动关的（连坐清理/重连覆盖/
		// 显式断开），remote_eof 是远端 shell 自己退出的。历史实现把两者都写成一行
		// "SSH 会话退出"，导致现场无法判断是节点断的、还是 bridge 自己清的。
		reason := sshx.SessionReasonRemoteEOF
		if s.IsClosed() {
			reason = "local_close"
		}
		exitErr := ""
		if err := s.Wait(); err != nil && !s.IsClosed() {
			exitErr = err.Error()
			log.Printf("[Bridge] SSH 会话退出(异常): case=%s err=%v", caseID, err)
		}
		evidence.Emit("WARN", "ssh.disconnected", "SSH 连接已结束", "", caseID, "", "", map[string]any{
			"reason":           reason,
			"exit_error":       exitErr,
			"duration_seconds": int64(sshx.AgeSeconds(s)),
		})

		// 主交互 shell 退出（HCI TMOUT=600）不等于整条 SSH 连接失效：
		// 交付拉取走独立 exec 通道，日志采集/进度每次也是 client.NewSession() 开新 channel，
		// 连接与通道都还能用。
		// 此时若照常 destroySession，正在跑的 12.8 GiB 拉取会被瞬间腰斩，
		// 且作业只能停在 needs_reauth（现场 24 次交付中断全部如此）；
		// 采集中被摘表则表现为后续 progress 全部 session_missing（2026-10-28 现场：
		// 600s EOF → 摘表 → 前端干等 60s 报"progress 超时（60s）"）。
		// 因此只要还有长任务占用 exec 通道，或刚才还在被业务使用，就只摘掉 shell，保留连接。
		if reason == sshx.SessionReasonRemoteEOF && (s.HasLongTask() || s.IsBusy()) {
			// 连接没坏，坏的是被 TMOUT 踢掉的 shell。优先在同一条连接上重建：
			// 用户可能只是去查资料了，不该被判"已断开、请重新认证"。
			if s.ShellRespawns.Load() < sshx.MaxShellRespawns {
				if reader, respawnErr := s.RespawnShell(); respawnErr == nil {
					respawnCount := s.ShellRespawns.Add(1)
					evidence.Emit("WARN", "session.shell_respawned", "交互 shell 被设备空闲超时踢出，已在同一条连接上重建", "", caseID, "", "", map[string]any{
						"duration_seconds":   int64(sshx.AgeSeconds(s)),
						"respawns":           respawnCount,
						"long_tasks":         s.LongTasks.Load(),
						"busy_age_seconds":   int64(s.BusyAgeSeconds()),
						"busy_grace_seconds": int(sshx.SessionBusyGrace().Seconds()),
					})
					models.SendMsg(ws, models.OutMessage{Type: "ssh_output", CaseID: caseID, Output: sshx.ShellRespawnNotice})
					// 继续转发新 shell 的输出。递归而非另起循环：onExit 语义保持不变，
					// 真正的连接结束仍会走到最外层那条路径。
					On_output_start(s, ws, reader, caseID, onExit)
					return
				}
			}
			evidence.Emit("WARN", "session.shell_eof_detached", "交互式 shell 已退出，连接保留给进行中的业务", "", caseID, "", "", map[string]any{
				"duration_seconds":   int64(sshx.AgeSeconds(s)),
				"long_tasks":         s.LongTasks.Load(),
				"busy_age_seconds":   int64(s.BusyAgeSeconds()),
				"busy_grace_seconds": int(sshx.SessionBusyGrace().Seconds()),
			})
			if ws != nil {
				models.SendMsg(ws, models.OutMessage{Type: "ssh_disconnected", CaseID: caseID})
			}
			return
		}

		if ws != nil {
			models.SendMsg(ws, models.OutMessage{Type: "ssh_disconnected", CaseID: caseID})
		}
		onExit(reason)
	}()
}

func NormalizeOutputText(raw string) string {
	if raw == "" || utf8.ValidString(raw) {
		return raw
	}
	decoded, _, err := transform.String(simplifiedchinese.GB18030.NewDecoder(), raw)
	if err != nil || decoded == "" {
		return strings.ToValidUTF8(raw, "\uFFFD")
	}
	return decoded
}

// truncateKeepTail 头尾都保留：安装/校验的结论（成功与否、节点数、失败原因）都在输出尾部，
// 只截头部恰好丢掉最需要的信息。
func truncateKeepTail(s string, headBytes, tailBytes int) string {
	if headBytes < 0 || tailBytes < 0 || len(s) <= headBytes+tailBytes {
		return s
	}
	head := s[:headBytes]
	for len(head) > 0 && !utf8.ValidString(head) {
		head = head[:len(head)-1]
	}
	tail := s[len(s)-tailBytes:]
	for len(tail) > 0 && !utf8.ValidString(tail) {
		tail = tail[1:]
	}
	return head + fmt.Sprintf("\n…[省略 %d 字节]…\n", len(s)-len(head)-len(tail)) + tail
}

// FormatOutputPreview 预览统一出口：先修编码，再按头尾保留截断。
func FormatOutputPreview(raw string, headBytes, tailBytes int) string {
	return truncateKeepTail(NormalizeOutputText(raw), headBytes, tailBytes)
}

// sshx.TruncateString 截断字符串到指定长度
