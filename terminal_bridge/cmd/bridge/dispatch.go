package main

import (
	"context"
	"encoding/json"
	"log"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/sftp"
	"terminal_bridge/internal/shared/models"
	"time"

	"golang.org/x/net/websocket"
)

func handle(b *app.Bridge, ws *websocket.Conn, customUI string) {
	cui := app.CustomUIHost(customUI)
	// connID 是本连接的唯一标识：没有它，connected/disconnected 与业务消息无法配对，
	// 现场只能看到"19 次连、17 次断"却不知道哪次断掉了哪条会话。
	connID := app.NewConnID()
	// role 由首条业务消息确定；端口探测连接永远停在 unknown（这是它们的正常形态）。
	role := app.ConnRoleUnknown
	openedAt := time.Now()
	// 订阅必须先于 logpush.Blog：否则"浏览器已连接"这条事件发生时本连接还不在 h.subs 里，
	// 它自己的处理函数都收不到，浏览器也就永远无法把它回采入库（实测丢过 14 条同类事件）。
	sub := logpush.DefaultHub.AddSubscriber(ws, cui)
	// 连接建立即留痕：浏览器是否在线决定了命令能否下发，是"命令没结果"类问题的第一判据。
	logpush.Blog("INFO", "bridge.connected", "浏览器已连接", "", "", "", cui, map[string]any{
		"origin": customUI, "custom_ui": cui, "remote": ws.RemoteAddr().String(),
		"conn_id": connID,
	})
	// 连接握手首包：让浏览器可快速识别"对端确实是 terminal_bridge"，
	// 用于端口冲突探测（避免误连占用主端口的陌生进程）。旧客户端会忽略未知类型。
	// detail 携带 instance_id：前端要用它申请"绑定实例"的日志直传令牌，
	// 没有它令牌只能绑定工单，无法阻止令牌被复制到另一台桥。
	models.SendMsg(ws, models.OutMessage{Type: "bridge_hello", Message: getVersion(), Detail: logpush.DefaultHub.InstanceID, CustomUI: cui})
	// ownedSessions 追踪 ssh_connect 显式创建的会话
	ownedSessions := app.NewOwnedSessionTracker()

	// WebSocket 保活心跳：定期发送 ping，防止中间层（负载均衡器、Traefik）空闲超时断开
	pingCtx, pingCancel := context.WithCancel(context.Background())
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		ticker := time.NewTicker(app.WebsocketPingInteval)
		defer ticker.Stop()
		for {
			select {
			case <-pingCtx.Done():
				return
			case <-ticker.C:
				models.SendMsg(ws, models.OutMessage{Type: "ping", Ping: time.Now().UnixMilli()})
			}
		}
	}()

	defer func() {
		pingCancel() // 停止心跳 goroutine
		<-pingDone   // 等待心跳 goroutine 退出
		// 先摘会话再记断连：own_keys 必须反映"这次断连真正带走了哪些会话"。
		// 2026-10-27 现场只能靠"报错比断连晚 266ms"反推，就是因为这里没有留痕。
		owned := ownedSessions.Drain()
		releasedKeys := make([]string, 0, len(owned))
		for key, ownedSession := range owned {
			if current := b.Get(key); current == ownedSession {
				// 不再销毁：改为释放持有权，会话进入空闲租约，等 TTL 回收。
				// WS 抖动/刷新不应带走正在被日志面板使用的会话
				//（2026-10-27 14:37:49 故障链的根因）。
				b.ReleaseSession(key, connID)
				releasedKeys = append(releasedKeys, key)
			}
		}
		// 断开留痕（含原因）：断连窗口内的命令结果会丢失，是"未收到执行结果"的高频根因。
		logpush.Blog("WARN", "bridge.disconnected", "浏览器已断开", "", sub.CaseID, "", cui, map[string]any{
			"custom_ui": cui, "remote": ws.RemoteAddr().String(), "reason": "connection_closed",
			"conn_id": connID, "role": role,
			"duration_seconds": int64(time.Since(openedAt).Seconds()),
			"owned_keys":       releasedKeys,
			"active_sessions":  b.SessionCount(),
		})
		logpush.DefaultHub.RemoveSubscriber(ws)
		models.WebsocketWriteLocks.Delete(ws)
		// 浏览器断开后立即清理凭据缓存，缩短密码/私钥在内存中的生命周期。
		b.ClearLastAuth(sub.CaseID)
		// 本连接发起的下载一并取消：浏览器都关了还继续拉完整个文件，白占客户带宽。
		if cancelled := sftp.CancelSftpTasksForConn(b, connID); cancelled > 0 {
			logpush.Blog("INFO", "sftp.tasks_cancelled_on_disconnect", "连接断开，已取消未完成的下载任务", "", sub.CaseID, "", cui, map[string]any{
				"conn_id": connID, "cancelled": cancelled,
			})
		}
	}()

	// R0c 注册式路由：连接级状态装入派发上下文，handler 经 ctx 缝读写。
	ctx := &app.WSContext{
		B: b, WS: ws, CUI: cui, ConnID: connID, Role: role,
		Sub: sub, Owned: ownedSessions,
	}

	for {
		var raw string
		if err := websocket.Message.Receive(ws, &raw); err != nil {
			log.Printf("[Bridge] WebSocket 接收结束: remote=%v err=%v", ws.RemoteAddr(), err)
			break
		}

		var msg models.InMessage
		if err := json.Unmarshal([]byte(raw), &msg); err != nil {
			log.Printf("[Bridge] 消息解析失败: remote=%v err=%v payload_bytes=%d", ws.RemoteAddr(), err, len(raw))
			continue
		}
		// 高频消息降为 DEBUG：一条 progress 轮询就产生一行，现场 219 行「收到消息」
		// 把真正的异常淹没了，而且它们全部没有 case_id / extra，云端也不可检索。
		// ping/pong 连 DEBUG 都不记：它们由 WS 心跳承担，不是业务事实。
		if msg.Type != "pong" && msg.Type != "ping" {
			logpush.Blog("DEBUG", "ws.message", "收到消息", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"msg_type": msg.Type, "container": msg.Container,
			})
		}
		// 连接角色只在首条业务消息时确定一次：探测连接停在 unknown 是正确结果，
		// 不能因为探测也发了 pong 就把它当成业务连接。
		if assigned := app.RoleOfMessageType(msg.Type); assigned != "" && ctx.Role == app.ConnRoleUnknown {
			ctx.Role = assigned
			logpush.Blog("INFO", "conn.role", "连接用途已确定", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"conn_id": connID, "role": ctx.Role,
			})
		}

		// R0c：注册式路由取代中央 switch；未知类型静默忽略（与旧行为一致）。
		if h := app.LookupWSRoute(msg.Type); h != nil {
			h(ctx, msg)
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────
