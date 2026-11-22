package main

// R0c 注册式路由：WS 消息按「谁的业务谁声明」分权，装配层只做汇总登记。
//
// 归属规则：消息类型与其 handler 写在所属域的 routes.go（导出 Routes()），
// 本文件只保留尚未归位的协议边缘。新增或改名一条消息不必再改这里（A5）。
//
// 仍留在本文件的两类：
//   - SSH/终端协议边缘（ssh_connect/ssh_input/ssh_exec_*/vm_console_op 等）：
//     需要会话生命周期与 AutoConnect；ssh_connect 里还有一处跨域调用
//     （logcollect.RecoverSpcRestoreIntents），端口化后才能整体归位 internal/terminal；
//   - list_sessions / bridge_log_token：前者是管理端对会话表的只读视图，
//     后者配置桥自身日志通道的凭据，都不属于任何具体业务域。
//
// handler 体与原 switch case 逐字等价：裸连接变量改经 ctx 缝、continue→return。

import (
	"log"
	"sync/atomic"
	"terminal_bridge/internal/acli"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/delivery"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/sftp"
	"terminal_bridge/internal/shared/metrics"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/shared/telemetry"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"terminal_bridge/internal/terminal"
	"time"
)

func init() {
	app.RegisterWSRoute("ssh_connect", handleSSHConnect)
	app.RegisterWSRoute("ssh_input", handleSSHInput)
	app.RegisterWSRoute("ssh_inject_command", handleSSHInjectCommand)
	app.RegisterWSRoute("resume", handleResume)
	app.RegisterWSRoute("pong", handlePong)
	app.RegisterWSRoute("ssh_disconnect", handleSSHDisconnect)
	app.RegisterWSRoute("list_sessions", handleListSessions)
	app.RegisterWSRoute("bridge_log_token", handleBridgeLogTokenRoute)

	// 已归位的域：消息类型由域自己声明（Routes()），这里只做汇总登记。
	// 新增或改名一条消息只需改对应域的 routes.go，不用再动本文件。
	app.RegisterWSRoutes(acli.Routes())
	app.RegisterWSRoutes(logcollect.Routes())
	app.RegisterWSRoutes(sftp.Routes())
	app.RegisterWSRoutes(delivery.Routes())
	app.RegisterWSRoutes(terminal.Routes())
}

func handleSSHConnect(ctx *app.WSContext, msg models.InMessage) {
	connectCtx, connectSpan := telemetry.StartClient(
		obslog.ContextFromMessage(msg), "terminal_bridge.ssh.connect",
		telemetry.String("case.id", msg.CaseID), telemetry.String("server.Address", msg.Host),
	)
	_ = connectCtx
	key := session.SessionKey(msg.CaseID, msg.NodeIP)
	// 同键覆盖必须走统一销毁入口：否则「旧会话被新连接顶掉」这件事
	// 在日志里完全不可见（只剩两条同键 ssh.connected，无法判断谁活了多久）。
	if old := ctx.Hub.Get(key); old != nil {
		// 长任务进行中禁止腰斩：用户看到"终端断开"就会去重连，而重连必然走到这里。
		// 无条件 destroySession 会把正在进行的 12.8 GiB 拉取直接打断到 needs_reauth，
		// 因此改为只摘表、保留连接，由长任务结束时的自愈逻辑关闭。
		ctx.Life.DestroyOrDetachSession(key, sshx.SessionReasonReconnect, ctx.ConnID)
		ctx.Owned.Remove(key)
	}
	// 如果没指定 nodeIP，也清理默认会话
	if msg.NodeIP == "" {
		defaultKey := session.SessionKey(msg.CaseID, "")
		if old := ctx.Hub.Get(defaultKey); old != nil {
			ctx.Life.DestroyOrDetachSession(defaultKey, sshx.SessionReasonReconnect, ctx.ConnID)
			ctx.Owned.Remove(defaultKey)
		}
	}

	session, err := sshx.NewSSHSession(msg)
	if err != nil {
		connectSpan.RecordError(err)
		connectSpan.SetStatus(telemetry.StatusError, "ssh_connect_failed")
		connectSpan.End()
		atomic.AddUint64(&metrics.PromMetrics.SshConnectionErrors, 1)
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return
	}
	stdout, err := session.Start()
	if err != nil {
		connectSpan.RecordError(err)
		connectSpan.SetStatus(telemetry.StatusError, "ssh_start_failed")
		connectSpan.End()
		atomic.AddUint64(&metrics.PromMetrics.SshConnectionErrors, 1)
		session.Close()
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return
	}
	ctx.Hub.Set(key, session)
	connectSpan.End()
	atomic.AddUint64(&metrics.PromMetrics.SshConnectionsTotal, 1)
	ctx.Owned.Set(key, session)
	// 启动应用层保活：终端空闲、日志采集长跑期间都要能尽早发现半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	// 本连接的键盘输入默认落到刚建立的这个会话（键含 @nodeIP）
	ctx.ActiveKey = key
	// 保存认证信息，供后续自动连接其他节点使用
	ctx.Hub.SetLastAuth(msg.CaseID, msg)
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
	go logcollect.RecoverSpcRestoreIntents(ctx.Hub, session, msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI)

	caseID := msg.CaseID
	terminal.On_output_start(session, ctx.WS, stdout, caseID, func(reason string) {
		if reason == sshx.SessionReasonRemoteEOF {
			ctx.Life.DestroySession(key, sshx.SessionReasonRemoteEOF, "")
			ctx.Owned.Remove(key)
			return
		}
		ctx.Hub.Remove(key)
		ctx.Owned.Remove(key)
	})
}

func handleSSHInput(ctx *app.WSContext, msg models.InMessage) {
	s, key := ctx.Hub.ResolveInput(msg, ctx.ActiveKey, ctx.Owned)
	if s == nil {
		// 以前这里查不到会话就静默返回，表现为"字打进去了、什么都不到、日志也干净"。
		// 现在必须留痕：先入库，再（节流）提示前端，避免再次无声无息。
		if ctx.LastInputRejectAt.IsZero() || time.Since(ctx.LastInputRejectAt) >= app.InputRejectNoticeInterval {
			ctx.LastInputRejectAt = time.Now()
			obslog.Emit("ERROR", "ssh_input.session_missing", "SSH 会话不存在，键盘输入已丢弃", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
				"input_type": "ssh_input", "active_key": ctx.ActiveKey,
				"exact_key": session.SessionKey(msg.CaseID, msg.NodeIP), "data_len": len(msg.Data),
			})
			models.SendMsg(ctx.WS, models.OutMessage{
				Type:    "ssh_input_rejected",
				CaseID:  msg.CaseID,
				Message: "SSH 会话不存在，输入未送达远端（请重新连接终端）",
			})
		}
		return
	}
	if key != session.SessionKey(msg.CaseID, "") && key != session.SessionKey(msg.CaseID, msg.NodeIP) {
		log.Printf("[Bridge] ssh_input 兜底命中会话: case=%s key=%s active=%s", msg.CaseID, key, ctx.ActiveKey)
	}
	// 键盘输入本身就是最强的使用证据：用户刚敲过键，shell 被 TMOUT 踢掉时
	// 必须重建而不是判"已断开"，否则"敲完命令去查资料"必然换来一次重连。
	s.MarkBusy()
	s.Send(msg.Data)
}

func handleSSHInjectCommand(ctx *app.WSContext, msg models.InMessage) {
	s, key := ctx.Hub.ResolveInput(msg, ctx.ActiveKey, ctx.Owned)
	if s == nil {
		if ctx.LastInputRejectAt.IsZero() || time.Since(ctx.LastInputRejectAt) >= app.InputRejectNoticeInterval {
			ctx.LastInputRejectAt = time.Now()
			obslog.Emit("ERROR", "ssh_input.session_missing", "SSH 会话不存在，命令注入已丢弃", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
				"input_type": "ssh_inject_command", "active_key": ctx.ActiveKey,
				"exact_key": session.SessionKey(msg.CaseID, msg.NodeIP),
			})
			models.SendMsg(ctx.WS, models.OutMessage{
				Type:    "ssh_input_rejected",
				CaseID:  msg.CaseID,
				Message: "SSH 会话不存在，命令未送达远端（请重新连接终端）",
			})
		}
		return
	}
	if key != session.SessionKey(msg.CaseID, "") && key != session.SessionKey(msg.CaseID, msg.NodeIP) {
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
		if current := ctx.Hub.Get(key); current == owned {
			// 显式断开与"页面被关掉"在日志里必须区分：前者是用户意图，
			// 后者是事故；两者都走同一入口，靠 reason 区分。
			ctx.Life.DestroySession(key, sshx.SessionReasonClientDisconnect, ctx.ConnID)
		}
	}
	models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})
}

func handleListSessions(ctx *app.WSContext, msg models.InMessage) {
	// 管理端枚举「custom-ui + bridge 已登录」的环境；只回会话摘要，不回凭据。
	obslog.Emit("INFO", "session.list", "管理端查询已登录 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{})
	models.SendMsg(ctx.WS, models.OutMessage{
		Type: "list_sessions_result", CaseID: msg.CaseID,
		Sessions: ctx.Hub.List(), TraceID: msg.TraceID, CustomUI: ctx.CUI,
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
