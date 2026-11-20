package main

// R0c 注册式路由：18 种 WS 消息的 handler 统一在装配层注册。
//
// 为什么暂不由各域 init() 自注册：app 目前仍 import obslog/terminal（搬迁前
// 既有依赖），域反向 import app 会成环；app→obslog 解环（接缝注入）属逻辑
// 收口阶段，届时各域注册迁回本域，装配层只留终端协议边缘。
//
// 终端/SSH 协议边缘（ssh_connect 跨 logcollect 还原意图、exec_process 跨
// logcollect 会话解析）同样待域间解环后归位 internal/terminal。
// handler 体与原 switch case 逐字等价：裸连接变量改经 ctx 缝、continue→return。

import (
	"fmt"
	"log"
	"sync/atomic"
	"time"

	"terminal_bridge/internal/acli"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/delivery"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/sftp"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/shared/telemetry"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/terminal"
)

func init() {
	app.RegisterWSRoute("ssh_connect", handleSSHConnect)
	app.RegisterWSRoute("ssh_input", handleSSHInput)
	app.RegisterWSRoute("ssh_inject_command", handleSSHInjectCommand)
	app.RegisterWSRoute("resume", handleResume)
	app.RegisterWSRoute("pong", handlePong)
	app.RegisterWSRoute("ssh_disconnect", handleSSHDisconnect)
	app.RegisterWSRoute("ssh_exec_command", handleSSHExecCommand)
	app.RegisterWSRoute("ssh_exec_process", handleSSHExecProcess)
	app.RegisterWSRoute("vm_console_op", handleVMConsoleOp)
	app.RegisterWSRoute("acli_sync", handleAcliSyncRoute)
	app.RegisterWSRoute("list_sessions", handleListSessions)
	app.RegisterWSRoute("bridge_log_token", handleBridgeLogTokenRoute)
	app.RegisterWSRoute("delivery_submit", handleDeliverySubmitRoute)
	app.RegisterWSRoute("delivery_status", handleDeliveryStatusRoute)
	app.RegisterWSRoute("delivery_resume", handleDeliveryResumeRoute)
	app.RegisterWSRoute("delivery_cancel", handleDeliveryCancelRoute)
	app.RegisterWSRoute("log_collect", handleLogCollectRoute)
	app.RegisterWSRoute("sftp_op", handleSftpOpRoute)
}

func handleSSHConnect(ctx *app.WSContext, msg models.InMessage) {
	connectCtx, connectSpan := telemetry.StartClient(
		obslog.ContextFromMessage(msg), "terminal_bridge.ssh.connect",
		telemetry.String("case.id", msg.CaseID), telemetry.String("server.Address", msg.Host),
	)
	_ = connectCtx
	key := app.SessionKey(msg.CaseID, msg.NodeIP)
	// 同键覆盖必须走统一销毁入口：否则「旧会话被新连接顶掉」这件事
	// 在日志里完全不可见（只剩两条同键 ssh.connected，无法判断谁活了多久）。
	if old := ctx.B.Get(key); old != nil {
		// 长任务进行中禁止腰斩：用户看到"终端断开"就会去重连，而重连必然走到这里。
		// 无条件 destroySession 会把正在进行的 12.8 GiB 拉取直接打断到 needs_reauth，
		// 因此改为只摘表、保留连接，由长任务结束时的自愈逻辑关闭。
		ctx.B.DestroyOrDetachSession(key, sshx.SessionReasonReconnect, ctx.ConnID)
		ctx.Owned.Remove(key)
	}
	// 如果没指定 nodeIP，也清理默认会话
	if msg.NodeIP == "" {
		defaultKey := app.SessionKey(msg.CaseID, "")
		if old := ctx.B.Get(defaultKey); old != nil {
			ctx.B.DestroyOrDetachSession(defaultKey, sshx.SessionReasonReconnect, ctx.ConnID)
			ctx.Owned.Remove(defaultKey)
		}
	}

	session, err := sshx.NewSSHSession(msg)
	if err != nil {
		connectSpan.RecordError(err)
		connectSpan.SetStatus(telemetry.StatusError, "ssh_connect_failed")
		connectSpan.End()
		atomic.AddUint64(&app.PromMetrics.SshConnectionErrors, 1)
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return
	}
	stdout, err := session.Start()
	if err != nil {
		connectSpan.RecordError(err)
		connectSpan.SetStatus(telemetry.StatusError, "ssh_start_failed")
		connectSpan.End()
		atomic.AddUint64(&app.PromMetrics.SshConnectionErrors, 1)
		session.Close()
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return
	}
	ctx.B.Set(key, session)
	connectSpan.End()
	atomic.AddUint64(&app.PromMetrics.SshConnectionsTotal, 1)
	ctx.Owned.Set(key, session)
	// 启动应用层保活：终端空闲、日志采集长跑期间都要能尽早发现半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	// 本连接的键盘输入默认落到刚建立的这个会话（键含 @nodeIP）
	ctx.ActiveKey = key
	// 保存认证信息，供后续自动连接其他节点使用
	ctx.B.SetLastAuth(msg.CaseID, msg)
	// 归属工单并回放近期日志（异常重传）
	ctx.Sub.BindCase(msg.CaseID)
	models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_connected", CaseID: msg.CaseID, CustomUI: ctx.CUI})
	// 会话建立事件与会话销毁事件成对出现：只有销毁没有建立时，
	// 无法区分「会话从没建过」和「建了又被清掉」——这两种处置完全不同。
	app.LogSessionCreated(session, key, ctx.ConnID, ctx.Role, "ssh_connect")
	obslog.Emit("INFO", "ssh.connected", "SSH 认证成功", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
		"user": msg.Username, "host": msg.Host, "port": msg.Port, "key": key,
		"conn_id": ctx.ConnID, "ctx.Role": ctx.Role,
	})
	// 抗崩溃（收口设计 §3.2）：bridge 重启后 spc 终态还原的 watcher 没了，
	// 凭持久意图账本把原件放回去。放进协程：集合性还原可能几十秒，
	// 不能阻塞连接回包；无意图时几乎零开销。
	go logcollect.RecoverSpcRestoreIntents(ctx.B, session, msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI)

	caseID := msg.CaseID
	terminal.On_output_start(session, ctx.WS, stdout, caseID, func(reason string) {
		if reason == sshx.SessionReasonRemoteEOF {
			ctx.B.DestroySession(key, sshx.SessionReasonRemoteEOF, "")
			ctx.Owned.Remove(key)
			return
		}
		ctx.B.Remove(key)
		ctx.Owned.Remove(key)
	})
}

func handleSSHInput(ctx *app.WSContext, msg models.InMessage) {
	s, key := ctx.B.ResolveInputSession(msg, ctx.ActiveKey, ctx.Owned)
	if s == nil {
		// 以前这里查不到会话就静默返回，表现为"字打进去了、什么都不到、日志也干净"。
		// 现在必须留痕：先入库，再（节流）提示前端，避免再次无声无息。
		if ctx.LastInputRejectAt.IsZero() || time.Since(ctx.LastInputRejectAt) >= app.InputRejectNoticeInterval {
			ctx.LastInputRejectAt = time.Now()
			obslog.Emit("ERROR", "ssh_input.session_missing", "SSH 会话不存在，键盘输入已丢弃", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
				"input_type": "ssh_input", "active_key": ctx.ActiveKey,
				"exact_key": app.SessionKey(msg.CaseID, msg.NodeIP), "data_len": len(msg.Data),
			})
			models.SendMsg(ctx.WS, models.OutMessage{
				Type:    "ssh_input_rejected",
				CaseID:  msg.CaseID,
				Message: "SSH 会话不存在，输入未送达远端（请重新连接终端）",
			})
		}
		return
	}
	if key != app.SessionKey(msg.CaseID, "") && key != app.SessionKey(msg.CaseID, msg.NodeIP) {
		log.Printf("[Bridge] ssh_input 兜底命中会话: case=%s key=%s active=%s", msg.CaseID, key, ctx.ActiveKey)
	}
	// 键盘输入本身就是最强的使用证据：用户刚敲过键，shell 被 TMOUT 踢掉时
	// 必须重建而不是判"已断开"，否则"敲完命令去查资料"必然换来一次重连。
	s.MarkBusy()
	s.Send(msg.Data)
}

func handleSSHInjectCommand(ctx *app.WSContext, msg models.InMessage) {
	s, key := ctx.B.ResolveInputSession(msg, ctx.ActiveKey, ctx.Owned)
	if s == nil {
		if ctx.LastInputRejectAt.IsZero() || time.Since(ctx.LastInputRejectAt) >= app.InputRejectNoticeInterval {
			ctx.LastInputRejectAt = time.Now()
			obslog.Emit("ERROR", "ssh_input.session_missing", "SSH 会话不存在，命令注入已丢弃", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
				"input_type": "ssh_inject_command", "active_key": ctx.ActiveKey,
				"exact_key": app.SessionKey(msg.CaseID, msg.NodeIP),
			})
			models.SendMsg(ctx.WS, models.OutMessage{
				Type:    "ssh_input_rejected",
				CaseID:  msg.CaseID,
				Message: "SSH 会话不存在，命令未送达远端（请重新连接终端）",
			})
		}
		return
	}
	if key != app.SessionKey(msg.CaseID, "") && key != app.SessionKey(msg.CaseID, msg.NodeIP) {
		log.Printf("[Bridge] ssh_inject_command 兜底命中会话: case=%s key=%s active=%s", msg.CaseID, key, ctx.ActiveKey)
	}
	s.InjectCommand(msg.Command)
}

func handleResume(ctx *app.WSContext, msg models.InMessage) {
	// P0-2: 浏览器重连时触发历史日志回放
	if msg.CaseID != "" {
		ctx.Sub.BindCase(msg.CaseID)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "resumed", CaseID: msg.CaseID, TraceID: msg.TraceID, CustomUI: ctx.CUI})
		log.Printf("[Bridge] 收到 resume 信号: case=%s 触发日志回放", msg.CaseID)
	}
}

func handlePong(ctx *app.WSContext, msg models.InMessage) {
	// WebSocket 保活心跳响应：客户端回复 pong，连接保持活跃
	// 不需要特殊处理，只需确保消息被识别即可
}

func handleSSHDisconnect(ctx *app.WSContext, msg models.InMessage) {
	// 关闭当前 WebSocket 所拥有的该工单全部会话，防止自动节点会话泄漏。
	for key, owned := range ctx.Owned.DrainCase(msg.CaseID) {
		if current := ctx.B.Get(key); current == owned {
			// 显式断开与"页面被关掉"在日志里必须区分：前者是用户意图，
			// 后者是事故；两者都走同一入口，靠 reason 区分。
			ctx.B.DestroySession(key, sshx.SessionReasonClientDisconnect, ctx.ConnID)
		}
	}
	models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})
}

func handleSSHExecCommand(ctx *app.WSContext, msg models.InMessage) {
	// 根据 nodeIP 路由到正确的 SSH 会话
	s, key := ctx.B.ResolveSession(msg)
	if s == nil && msg.NodeIP != "" {
		// 自动连接新节点
		s = ctx.B.AutoConnectNode(ctx.WS, msg, ctx.Owned, ctx.ConnID, ctx.Role)
		key = app.SessionKey(msg.CaseID, msg.NodeIP)
	}
	// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话
	if s == nil && ctx.Sub.BoundCase() != "" {
		if fb := ctx.B.Get(app.SessionKey(ctx.Sub.BoundCase(), "")); fb != nil {
			s = fb
			key = app.SessionKey(ctx.Sub.BoundCase(), "")
			log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", ctx.Sub.BoundCase(), key)
			// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
			obslog.Emit("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, ctx.Sub.BoundCase(), msg.NodeIP, ctx.CUI, map[string]any{
				"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
			})
		}
	}
	if s == nil {
		obslog.Emit("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "key": app.SessionKey(msg.CaseID, msg.NodeIP),
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
	atomic.AddUint64(&app.PromMetrics.ExecCommandsTotal, 1)
	go func() {
		result := <-resultChan
		output := result.Output
		exitCode := result.ExitCode
		if result.Timeout {
			output = "execution timeout"
		}
		if result.Timeout || exitCode != 0 {
			atomic.AddUint64(&app.PromMetrics.ExecCommandErrors, 1)
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

func handleSSHExecProcess(ctx *app.WSContext, msg models.InMessage) {
	// 会话路由与 log_collect/acli_sync 对齐：custom-ui 登录建会话时用的
	// case_id 可能是工单号或 terminal-only，管理端下发只需要按 node_ip
	// 复用同一 bridge 进程里已存在的 SSH 会话，不再新建连接。
	s, key, _ := logcollect.ResolveLogCollectSession(ctx.B, msg)
	if s == nil && msg.NodeIP != "" {
		s = ctx.B.AutoConnectNode(ctx.WS, msg, ctx.Owned, ctx.ConnID, ctx.Role)
		key = app.SessionKey(msg.CaseID, msg.NodeIP)
	}
	// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话。
	// 因为一次 WebSocket 连接在 ssh_connect 后只存在一个会话，可据此兜底，
	// 避免空 case_id 导致 exec.session_missing、回采失败且无 DB 记录。
	if s == nil && ctx.Sub.BoundCase() != "" {
		if fb := ctx.B.Get(app.SessionKey(ctx.Sub.BoundCase(), "")); fb != nil {
			s = fb
			key = app.SessionKey(ctx.Sub.BoundCase(), "")
			log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", ctx.Sub.BoundCase(), key)
			// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
			obslog.Emit("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, ctx.Sub.BoundCase(), msg.NodeIP, ctx.CUI, map[string]any{
				"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
			})
		}
	}
	if s == nil {
		obslog.Emit("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令(隔离通道)", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "key": app.SessionKey(msg.CaseID, msg.NodeIP),
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

	atomic.AddUint64(&app.PromMetrics.ExecCommandsTotal, 1)
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
		requestedTimeout = terminal.CommandTimeout(msg.Timeout)
	}
	go terminal.ExecCommandIsolated(s, ctx.WS, req, requestedTimeout, msg.OutputFilters)
}

func handleVMConsoleOp(ctx *app.WSContext, msg models.InMessage) {
	// 虚拟机控制台截图固定操作通道：入口即校验，任何非法输入都不触发命令执行。
	// 刻意不复用 ctx.Sub.BoundCase() 兜底会话——截图必须精确落在 node_ip 指定的宿主机上，
	// 找不到目标会话时 fail-closed，绝不把截图命令发到错误节点。
	vmReq, errType := terminal.ParseVMConsoleRequest(msg, ctx.CUI)
	if errType != "" {
		obslog.Emit("ERROR", "vm_console.rejected", "vm_console_op 入口校验失败，已拒绝执行", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "operation": msg.Operation, "capture_id": msg.CaptureID, "error_type": errType,
		})
		rejected := terminal.VmConsoleResultMessage(vmReq, -1, msg.TraceID, "", 0)
		rejected.TraceID = msg.TraceID
		rejected.ErrorType = errType
		models.SendMsg(ctx.WS, rejected)
		return
	}

	s, key := ctx.B.ResolveSession(msg)
	if s == nil && msg.NodeIP != "" {
		// 复用既有自动连接：使用最近一次 ssh_connect 的认证信息连接目标节点。
		s = ctx.B.AutoConnectNode(ctx.WS, msg, ctx.Owned, ctx.ConnID, ctx.Role)
		key = app.SessionKey(msg.CaseID, msg.NodeIP)
	}
	if s == nil {
		atomic.AddUint64(&app.PromMetrics.VmConsoleOpErrors, 1)
		obslog.Emit("ERROR", "vm_console.session_missing", "SSH 会话不存在，无法执行控制台固定操作", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
			"exec_id": msg.ExecID, "operation": msg.Operation, "key": app.SessionKey(msg.CaseID, msg.NodeIP),
		})
		missing := terminal.VmConsoleResultMessage(vmReq, -1, msg.TraceID, "", 0)
		missing.TraceID = msg.TraceID
		missing.ErrorType = terminal.VmConsoleErrSessionMissing
		models.SendMsg(ctx.WS, missing)
		return
	}

	obslog.Emit("INFO", "vm_console.request", "收到虚拟机控制台固定操作请求", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
		"exec_id": vmReq.ExecID, "operation": vmReq.Operation, "capture_id": vmReq.CaptureID,
		"host_node_id": vmReq.HostNodeID, "vm_id": vmReq.VMID, "key": key,
		"timeout_seconds": vmReq.TimeoutSeconds,
	})
	atomic.AddUint64(&app.PromMetrics.VmConsoleOpsTotal, 1)
	go terminal.RunVMConsoleOp(s, func(result models.OutMessage) { models.SendMsg(ctx.WS, result) }, vmReq)
}

func handleAcliSyncRoute(ctx *app.WSContext, msg models.InMessage) {
	go acli.HandleAcliSync(ctx.B, ctx.WS, msg, ctx.CUI)
}

func handleListSessions(ctx *app.WSContext, msg models.InMessage) {
	// 管理端枚举「custom-ui + bridge 已登录」的环境；只回会话摘要，不回凭据。
	obslog.Emit("INFO", "session.list", "管理端查询已登录 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{})
	models.SendMsg(ctx.WS, models.OutMessage{
		Type: "list_sessions_result", CaseID: msg.CaseID,
		Sessions: ctx.B.ListSessions(), TraceID: msg.TraceID, CustomUI: ctx.CUI,
	})
}

func handleBridgeLogTokenRoute(ctx *app.WSContext, msg models.InMessage) {
	// 前端下发的直传凭据：拿到即启动后台 worker，此后桥侧日志不再依赖浏览器。
	// 令牌为空/endpoint 非法时 configure 直接返回，前端保持原有的浏览器回采链路。
	obslog.LogUpload.Configure(msg.LogToken, msg.LogEndpoint, msg.CaseID, obslog.DefaultHub.InstanceID)
	models.SendMsg(ctx.WS, models.OutMessage{Type: "bridge_log_token_ack", CaseID: msg.CaseID, Status: "ok"})
	obslog.Emit("INFO", "log_upload.token_received", "收到桥日志直传凭据", msg.TraceID, msg.CaseID, "", ctx.CUI, map[string]any{
		"endpoint":     msg.LogEndpoint,
		"token_length": len(msg.LogToken),
	})
}

func handleDeliverySubmitRoute(ctx *app.WSContext, msg models.InMessage) {
	// 交付作业化：立刻返回 job_id，执行权交给 bridge，前端只轮询进度。
	go delivery.HandleDeliverySubmit(ctx.B, ctx.WS, msg, ctx.CUI)
}

func handleDeliveryStatusRoute(ctx *app.WSContext, msg models.InMessage) {
	go delivery.HandleDeliveryStatus(ctx.B, ctx.WS, msg, ctx.CUI)
}

func handleDeliveryResumeRoute(ctx *app.WSContext, msg models.InMessage) {
	go delivery.HandleDeliveryResume(ctx.B, ctx.WS, msg, ctx.CUI)
}

func handleDeliveryCancelRoute(ctx *app.WSContext, msg models.InMessage) {
	go delivery.HandleDeliveryCancel(ctx.B, ctx.WS, msg, ctx.CUI)
}

func handleLogCollectRoute(ctx *app.WSContext, msg models.InMessage) {
	go logcollect.HandleLogCollect(ctx.B, ctx.WS, msg, ctx.CUI)
}

func handleSftpOpRoute(ctx *app.WSContext, msg models.InMessage) {
	// 下载在独立 goroutine 里跑：一个 5 GiB 的流不能把交互式 shell 的消息循环堵住。
	go sftp.HandleSftpOp(ctx.B, ctx.WS, msg, ctx.CUI, ctx.ConnID)
}
