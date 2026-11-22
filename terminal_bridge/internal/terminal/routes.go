package terminal

import (
	"fmt"
	"log"
	"sync/atomic"
	"time"

	"terminal_bridge/internal/shared/metrics"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/shared/telemetry"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"terminal_bridge/internal/wsctx"
)

// WS 路由声明：终端交互（标记式 EXEC / 隔离 EXEC / 虚拟机控制台）归本域所有。
// 装配层只做汇总登记，新增或改名一条消息只改本文件（A5）。
func Routes() []wsctx.Route {
	return []wsctx.Route{
		{Type: "ssh_exec_command", Handler: handleExecCommand},
		{Type: "ssh_exec_process", Handler: handleExecProcess},
		{Type: "vm_console_op", Handler: handleVMConsole},
	}
}

func handleExecCommand(ctx *wsctx.Context, msg models.InMessage) {
	// 根据 nodeIP 路由到正确的 SSH 会话
	s, key := ctx.Hub.Resolve(msg)
	if s == nil && msg.NodeIP != "" {
		// 自动连接新节点
		s = ctx.Life.AutoConnectNode(ctx.WS, msg, ctx.Owned, ctx.ConnID, ctx.Role)
		key = session.SessionKey(msg.CaseID, msg.NodeIP)
	}
	// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话
	if s == nil && ctx.Sub.BoundCase() != "" {
		if fb := ctx.Hub.Get(session.SessionKey(ctx.Sub.BoundCase(), "")); fb != nil {
			s = fb
			key = session.SessionKey(ctx.Sub.BoundCase(), "")
			log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", ctx.Sub.BoundCase(), key)
			// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
			obslog.Emit("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, ctx.Sub.BoundCase(), msg.NodeIP, ctx.CUI, map[string]any{
				"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
			})
		}
	}
	if s == nil {
		obslog.Emit("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "key": session.SessionKey(msg.CaseID, msg.NodeIP),
			"sub_case_id": ctx.Sub.BoundCase(), "has_fallback_target": ctx.Sub.BoundCase() != "",
		})
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Output: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
			TraceID: msg.TraceID, CustomUI: ctx.CUI,
		})
		return
	}
	if msg.ExecID == "" {
		// 参数缺失属"命令被拒绝"：此前只回消息不打日志，云端完全无痕。
		obslog.Emit("ERROR", "exec.rejected", "命令被拒绝：缺少 exec_id 参数", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"error_type": "missing_exec_id", "channel": "marker", "node_ip": msg.NodeIP,
			"container": msg.Container, "cmd_len": len(msg.Command),
		})
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Output: "缺少 exec_id 参数", ExitCode: -1,
			TraceID: msg.TraceID, CustomUI: ctx.CUI,
		})
		return
	}
	if msg.Command == "" {
		obslog.Emit("ERROR", "exec.rejected", "命令被拒绝：缺少 command 参数", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"error_type": "missing_command", "channel": "marker", "exec_id": msg.ExecID,
			"node_ip": msg.NodeIP, "container": msg.Container,
		})
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Output: "缺少 command 参数", ExitCode: -1,
			TraceID: msg.TraceID, CustomUI: ctx.CUI,
		})
		return
	}

	// 包装容器命令
	wrappedCmd := sshx.WrapContainerCommand(msg.Command, msg.Container, s.Simulation)
	obslog.Emit("INFO", "exec.start", "开始执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
		"exec_id": msg.ExecID, "key": key, "container": msg.Container, "cmd_len": len(wrappedCmd),
	})
	log.Printf("[Bridge] EXEC_START: key=%s node=%s container=%s exec_id=%s cmd_len=%d",
		key, msg.NodeIP, msg.Container, msg.ExecID, len(wrappedCmd))
	log.Printf("[Bridge] EXEC_CMD: sha256=%s redacted=%q", obslog.CommandSHA256(wrappedCmd), obslog.RedactCommand(wrappedCmd))

	resultChan := s.ExecCommand(wrappedCmd, msg.ExecID, 60*time.Second)
	atomic.AddUint64(&metrics.PromMetrics.ExecCommandsTotal, 1)
	go func() {
		result := <-resultChan
		output := result.Output
		exitCode := result.ExitCode
		if result.Timeout {
			output = "execution timeout"
		}
		if result.Timeout || exitCode != 0 {
			atomic.AddUint64(&metrics.PromMetrics.ExecCommandErrors, 1)
		}
		outLen := len(output)
		exitInfo := fmt.Sprintf("exit=%d", exitCode)
		errType := result.ErrorType
		if errType == "" && !result.Timeout {
			errType = sshx.ClassifyExecFailure(exitCode, output)
		}
		if result.Timeout {
			errType = "timeout"
			exitInfo = "exit=TIMEOUT"
		}
		logLevel := "INFO"
		if result.Timeout || exitCode != 0 {
			logLevel = "ERROR"
		}

		// P0-1: 增强日志完整性 - 回采命令执行的完整输出
		// 输出过长时截断为预览（避免日志字段过大），但完整记录长度和退出码
		obslog.Emit("INFO", "exec.output", "命令执行输出", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id":          msg.ExecID,
			"exit_code":        exitCode,
			"timeout":          result.Timeout,
			"error_type":       errType,
			"output_len":       outLen,
			"output_sha256":    obslog.CommandSHA256(output),
			"command_redacted": obslog.RedactCommand(wrappedCmd),
			"command_sha256":   obslog.CommandSHA256(wrappedCmd),
		})

		obslog.Emit(logLevel, "exec.done", "命令执行完成", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "exit_code": exitCode, "timeout": result.Timeout,
			"error_type": errType, "output_len": outLen,
		})
		log.Printf("[Bridge] EXEC_DONE: exec_id=%s node=%s %s error_type=%s output_len=%d",
			msg.ExecID, msg.NodeIP, exitInfo, errType, outLen)
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Output: output, ExitCode: exitCode, ErrorType: errType,
			TraceID: msg.TraceID, CustomUI: ctx.CUI,
		})
	}()
}

func handleExecProcess(ctx *wsctx.Context, msg models.InMessage) {
	// 会话路由与 log_collect/acli_sync 对齐：custom-ui 登录建会话时用的
	// case_id 可能是工单号或 terminal-only，管理端下发只需要按 node_ip
	// 复用同一 bridge 进程里已存在的 SSH 会话，不再新建连接。
	s, key, _ := ctx.Hub.ResolveForNode(msg)
	if s == nil && msg.NodeIP != "" {
		s = ctx.Life.AutoConnectNode(ctx.WS, msg, ctx.Owned, ctx.ConnID, ctx.Role)
		key = session.SessionKey(msg.CaseID, msg.NodeIP)
	}
	// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话。
	// 因为一次 WebSocket 连接在 ssh_connect 后只存在一个会话，可据此兜底，
	// 避免空 case_id 导致 exec.session_missing、回采失败且无 DB 记录。
	if s == nil && ctx.Sub.BoundCase() != "" {
		if fb := ctx.Hub.Get(session.SessionKey(ctx.Sub.BoundCase(), "")); fb != nil {
			s = fb
			key = session.SessionKey(ctx.Sub.BoundCase(), "")
			log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", ctx.Sub.BoundCase(), key)
			// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
			obslog.Emit("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, ctx.Sub.BoundCase(), msg.NodeIP, ctx.CUI, map[string]any{
				"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
			})
		}
	}
	if s == nil {
		obslog.Emit("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令(隔离通道)", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "key": session.SessionKey(msg.CaseID, msg.NodeIP),
			"sub_case_id": ctx.Sub.BoundCase(), "has_fallback_target": ctx.Sub.BoundCase() != "",
		})
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Stderr: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
			TraceID: msg.TraceID, CustomUI: ctx.CUI,
		})
		return
	}
	if msg.ExecID == "" {
		obslog.Emit("ERROR", "exec.rejected", "命令被拒绝：缺少 exec_id 参数", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"error_type": "missing_exec_id", "channel": "isolated", "node_ip": msg.NodeIP,
			"container": msg.Container, "cmd_len": len(msg.Command),
		})
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Stderr: "缺少 exec_id 参数", ExitCode: -1,
			TraceID: msg.TraceID, CustomUI: ctx.CUI,
		})
		return
	}
	if msg.Command == "" {
		obslog.Emit("ERROR", "exec.rejected", "命令被拒绝：缺少 command 参数", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"error_type": "missing_command", "channel": "isolated", "exec_id": msg.ExecID,
			"node_ip": msg.NodeIP, "container": msg.Container,
		})
		models.SendMsg(ctx.WS, models.OutMessage{
			Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
			Stderr: "缺少 command 参数", ExitCode: -1,
		})
		return
	}

	// 包装容器命令
	wrappedCmd := sshx.WrapContainerCommand(msg.Command, msg.Container, s.Simulation)
	log.Printf("[Bridge] EXEC_ISOLATED_START: key=%s node=%s container=%s exec_id=%s cmd_len=%d",
		key, msg.NodeIP, msg.Container, msg.ExecID, len(wrappedCmd))
	commandHash := obslog.CommandSHA256(wrappedCmd)

	// P0: 记录命令发起（包含 trace_id）
	obslog.Emit("INFO", "exec.request", "收到命令执行请求", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
		"exec_id":          msg.ExecID,
		"command_redacted": obslog.RedactCommand(wrappedCmd),
		"command_sha256":   commandHash,
		"container":        msg.Container,
		"trace_id":         msg.TraceID,
	})

	atomic.AddUint64(&metrics.PromMetrics.ExecCommandsTotal, 1)
	receiveCtx, receiveSpan := telemetry.Start(
		obslog.ContextFromMessage(msg), "terminal_bridge.websocket.receive",
		telemetry.String("exec.id", msg.ExecID), telemetry.String("message.type", msg.Type),
	)
	receiveSpan.End()
	req := models.ExecRequestContext{
		Context: receiveCtx, CaseID: msg.CaseID, ConversationID: msg.ConversationID,
		ExecID: msg.ExecID, ToolCallID: msg.ToolCallID, TraceID: msg.TraceID,
		Traceparent: msg.Traceparent, Tracestate: msg.Tracestate, TestRunID: msg.TestRunID, NodeIP: msg.NodeIP,
		Container: msg.Container, CustomUI: ctx.CUI, Command: wrappedCmd,
		CommandRedacted: obslog.RedactCommand(wrappedCmd), CommandSHA256: commandHash,
		Pty: msg.Pty,
	}
	var requestedTimeout time.Duration
	if msg.Timeout > 0 {
		requestedTimeout = CommandTimeout(msg.Timeout)
	}
	go ExecCommandIsolated(s, ctx.WS, req, requestedTimeout, msg.OutputFilters)
}

func handleVMConsole(ctx *wsctx.Context, msg models.InMessage) {
	// 虚拟机控制台截图固定操作通道：入口即校验，任何非法输入都不触发命令执行。
	// 刻意不复用 ctx.Sub.BoundCase() 兜底会话——截图必须精确落在 node_ip 指定的宿主机上，
	// 找不到目标会话时 fail-closed，绝不把截图命令发到错误节点。
	vmReq, errType := ParseVMConsoleRequest(msg, ctx.CUI)
	if errType != "" {
		obslog.Emit("ERROR", "vm_console.rejected", "vm_console_op 入口校验失败，已拒绝执行", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "operation": msg.Operation, "capture_id": msg.CaptureID, "error_type": errType,
		})
		rejected := VmConsoleResultMessage(vmReq, -1, msg.TraceID, "", 0)
		rejected.TraceID = msg.TraceID
		rejected.ErrorType = errType
		models.SendMsg(ctx.WS, rejected)
		return
	}

	s, key := ctx.Hub.Resolve(msg)
	if s == nil && msg.NodeIP != "" {
		// 复用既有自动连接：使用最近一次 ssh_connect 的认证信息连接目标节点。
		s = ctx.Life.AutoConnectNode(ctx.WS, msg, ctx.Owned, ctx.ConnID, ctx.Role)
		key = session.SessionKey(msg.CaseID, msg.NodeIP)
	}
	if s == nil {
		atomic.AddUint64(&metrics.PromMetrics.VmConsoleOpErrors, 1)
		obslog.Emit("ERROR", "vm_console.session_missing", "SSH 会话不存在，无法执行控制台固定操作", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "operation": msg.Operation, "key": session.SessionKey(msg.CaseID, msg.NodeIP),
		})
		missing := VmConsoleResultMessage(vmReq, -1, msg.TraceID, "", 0)
		missing.TraceID = msg.TraceID
		missing.ErrorType = VmConsoleErrSessionMissing
		models.SendMsg(ctx.WS, missing)
		return
	}

	obslog.Emit("INFO", "vm_console.request", "收到虚拟机控制台固定操作请求", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
		"exec_id": vmReq.ExecID, "operation": vmReq.Operation, "capture_id": vmReq.CaptureID,
		"host_node_id": vmReq.HostNodeID, "vm_id": vmReq.VMID, "key": key,
		"timeout_seconds": vmReq.TimeoutSeconds,
	})
	atomic.AddUint64(&metrics.PromMetrics.VmConsoleOpsTotal, 1)
	go RunVMConsoleOp(s, func(result models.OutMessage) { models.SendMsg(ctx.WS, result) }, vmReq)
}
