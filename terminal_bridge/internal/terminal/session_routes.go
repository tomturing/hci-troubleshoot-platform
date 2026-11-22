package terminal

import (
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

// SessionDeps 是会话族 handler 需要的外部能力。
//
// 为什么用函数而不是让终端域 import 采集域：SPC 还原意图是采集域的语义，
// 终端域只表达「会话已建立，请补做还原」这一意图。反过来 import 会立刻产生
// 域间依赖边，撞 import 门禁禁令 5（基线已清空）。nil 表示不接（测试或未启用）。
type SessionDeps struct {
	RecoverSpcRestore func(hub *session.Hub, s *sshx.SSHSession, traceID, caseID, nodeIP, customUI string)
}

// SessionRoutes 返回会话族路由：连接建立、键盘输入、命令注入、重连续传、
// 心跳、断开与会话列表。装配层只注入 SessionDeps 并汇总登记（A5）。
func SessionRoutes(deps SessionDeps) []wsctx.Route {
	return []wsctx.Route{
		{Type: "ssh_connect", Handler: func(ctx *wsctx.Context, msg models.InMessage) { handleConnect(ctx, msg, deps) }},
		{Type: "ssh_input", Handler: handleInput},
		{Type: "ssh_inject_command", Handler: handleInjectCommand},
		{Type: "resume", Handler: handleResume},
		{Type: "pong", Handler: handlePong},
		{Type: "ssh_disconnect", Handler: handleDisconnect},
		{Type: "list_sessions", Handler: handleListSessions},
	}
}

// handleConnect 建立一条到节点的 SSH 会话，并把连接级状态（活跃键、认证快照、
// 工单归属）一并挂到这条 WebSocket 上。
func handleConnect(ctx *wsctx.Context, msg models.InMessage, deps SessionDeps) {
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

	sess, err := sshx.NewSSHSession(msg)
	if err != nil {
		connectSpan.RecordError(err)
		connectSpan.SetStatus(telemetry.StatusError, "ssh_connect_failed")
		connectSpan.End()
		atomic.AddUint64(&metrics.PromMetrics.SshConnectionErrors, 1)
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return
	}
	stdout, err := sess.Start()
	if err != nil {
		connectSpan.RecordError(err)
		connectSpan.SetStatus(telemetry.StatusError, "ssh_start_failed")
		connectSpan.End()
		atomic.AddUint64(&metrics.PromMetrics.SshConnectionErrors, 1)
		sess.Close()
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return
	}
	ctx.Hub.Set(key, sess)
	connectSpan.End()
	atomic.AddUint64(&metrics.PromMetrics.SshConnectionsTotal, 1)
	ctx.Owned.Set(key, sess)
	// 启动应用层保活：终端空闲、日志采集长跑期间都要能尽早发现半开连接。
	sess.StartKeepAlive(sshx.SshKeepAliveInterval())
	// 本连接的键盘输入默认落到刚建立的这个会话（键含 @nodeIP）
	ctx.ActiveKey = key
	// 保存认证信息，供后续自动连接其他节点使用
	ctx.Hub.SetLastAuth(msg.CaseID, msg)
	// 归属工单并回放近期日志（异常重传）
	ctx.Sub.BindCase(msg.CaseID)
	models.SendMsg(ctx.WS, models.OutMessage{Type: "ssh_connected", CaseID: msg.CaseID, CustomUI: ctx.CUI})
	// 会话建立事件与会话销毁事件成对出现：只有销毁没有建立时，
	// 无法区分「会话从没建过」和「建了又被清掉」——这两种处置完全不同。
	session.LogCreated(sess, key, ctx.ConnID, ctx.Role, "ssh_connect")
	obslog.Emit("INFO", "ssh.connected", "SSH 认证成功", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{
		"user": msg.Username, "host": msg.Host, "port": msg.Port, "key": key,
		"conn_id": ctx.ConnID, "ctx.Role": ctx.Role,
	})
	// 抗崩溃（收口设计 §3.2）：bridge 重启后 spc 终态还原的 watcher 没了，
	// 凭持久意图账本把原件放回去。放进协程：集合性还原可能几十秒，
	// 不能阻塞连接回包；无意图时几乎零开销。
	if deps.RecoverSpcRestore != nil {
		go deps.RecoverSpcRestore(ctx.Hub, sess, msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI)
	}

	caseID := msg.CaseID
	On_output_start(sess, ctx.WS, stdout, caseID, func(reason string) {
		if reason == sshx.SessionReasonRemoteEOF {
			ctx.Life.DestroySession(key, sshx.SessionReasonRemoteEOF, "")
			ctx.Owned.Remove(key)
			return
		}
		ctx.Hub.Remove(key)
		ctx.Owned.Remove(key)
	})
}

func handleInput(ctx *wsctx.Context, msg models.InMessage) {
	s, key := ctx.Hub.ResolveInput(msg, ctx.ActiveKey, ctx.Owned)
	if s == nil {
		// 以前这里查不到会话就静默返回，表现为"字打进去了、什么都不到、日志也干净"。
		// 现在必须留痕：先入库，再（节流）提示前端，避免再次无声无息。
		if ctx.LastInputRejectAt.IsZero() || time.Since(ctx.LastInputRejectAt) >= wsctx.InputRejectNoticeInterval {
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

func handleInjectCommand(ctx *wsctx.Context, msg models.InMessage) {
	s, key := ctx.Hub.ResolveInput(msg, ctx.ActiveKey, ctx.Owned)
	if s == nil {
		if ctx.LastInputRejectAt.IsZero() || time.Since(ctx.LastInputRejectAt) >= wsctx.InputRejectNoticeInterval {
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

func handleResume(ctx *wsctx.Context, msg models.InMessage) {
	// P0-2: 浏览器重连时触发历史日志回放
	if msg.CaseID != "" {
		ctx.Sub.BindCase(msg.CaseID)
		models.SendMsg(ctx.WS, models.OutMessage{Type: "resumed", CaseID: msg.CaseID, TraceID: msg.TraceID, CustomUI: ctx.CUI})
		log.Printf("[Bridge] 收到 resume 信号: case=%s 触发日志回放", msg.CaseID)
	}
}

func handlePong(ctx *wsctx.Context, msg models.InMessage) {
	// WebSocket 保活心跳响应：客户端回复 pong，连接保持活跃
	// 不需要特殊处理，只需确保消息被识别即可
}

func handleDisconnect(ctx *wsctx.Context, msg models.InMessage) {
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

func handleListSessions(ctx *wsctx.Context, msg models.InMessage) {
	// 管理端枚举「custom-ui + bridge 已登录」的环境；只回会话摘要，不回凭据。
	obslog.Emit("INFO", "session.list", "管理端查询已登录 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, ctx.CUI, map[string]any{})
	models.SendMsg(ctx.WS, models.OutMessage{
		Type: "list_sessions_result", CaseID: msg.CaseID,
		Sessions: ctx.Hub.List(), TraceID: msg.TraceID, CustomUI: ctx.CUI,
	})
}
