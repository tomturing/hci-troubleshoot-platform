package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync/atomic"
	"terminal_bridge/internal/acli"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/terminal"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
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
		if cancelled := cancelSftpTasksForConn(b, connID); cancelled > 0 {
			logpush.Blog("INFO", "sftp.tasks_cancelled_on_disconnect", "连接断开，已取消未完成的下载任务", "", sub.CaseID, "", cui, map[string]any{
				"conn_id": connID, "cancelled": cancelled,
			})
		}
	}()

	// activeKey 记录本连接最近一次 ssh_connect 建立的会话键（含 @nodeIP）。
	// 键盘输入属于"这条连接的这块终端"，用它兜底比按全局 caseID 猜更可靠。
	var activeKey string
	// 输入被丢弃的提示节流：用户连按回车时不必重复回包刷屏。
	var lastInputRejectAt time.Time

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
		if assigned := app.RoleOfMessageType(msg.Type); assigned != "" && role == app.ConnRoleUnknown {
			role = assigned
			logpush.Blog("INFO", "conn.role", "连接用途已确定", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"conn_id": connID, "role": role,
			})
		}

		switch msg.Type {

		case "ssh_connect":
			connectCtx, connectSpan := otel.Tracer("terminal_bridge").Start(
				logpush.ContextFromMessage(msg), "terminal_bridge.ssh.connect",
				trace.WithSpanKind(trace.SpanKindClient),
				trace.WithAttributes(attribute.String("case.id", msg.CaseID), attribute.String("server.Address", msg.Host)),
			)
			_ = connectCtx
			key := app.SessionKey(msg.CaseID, msg.NodeIP)
			// 同键覆盖必须走统一销毁入口：否则「旧会话被新连接顶掉」这件事
			// 在日志里完全不可见（只剩两条同键 ssh.connected，无法判断谁活了多久）。
			if old := b.Get(key); old != nil {
				// 长任务进行中禁止腰斩：用户看到"终端断开"就会去重连，而重连必然走到这里。
				// 无条件 destroySession 会把正在进行的 12.8 GiB 拉取直接打断到 needs_reauth，
				// 因此改为只摘表、保留连接，由长任务结束时的自愈逻辑关闭。
				b.DestroyOrDetachSession(key, sshx.SessionReasonReconnect, connID)
				ownedSessions.Remove(key)
			}
			// 如果没指定 nodeIP，也清理默认会话
			if msg.NodeIP == "" {
				defaultKey := app.SessionKey(msg.CaseID, "")
				if old := b.Get(defaultKey); old != nil {
					b.DestroyOrDetachSession(defaultKey, sshx.SessionReasonReconnect, connID)
					ownedSessions.Remove(defaultKey)
				}
			}

			session, err := sshx.NewSSHSession(msg)
			if err != nil {
				connectSpan.RecordError(err)
				connectSpan.SetStatus(codes.Error, "ssh_connect_failed")
				connectSpan.End()
				atomic.AddUint64(&app.PromMetrics.SshConnectionErrors, 1)
				message, detail := sshx.BuildSSHError(err)
				models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				continue
			}
			stdout, err := session.Start()
			if err != nil {
				connectSpan.RecordError(err)
				connectSpan.SetStatus(codes.Error, "ssh_start_failed")
				connectSpan.End()
				atomic.AddUint64(&app.PromMetrics.SshConnectionErrors, 1)
				session.Close()
				message, detail := sshx.BuildSSHError(err)
				models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
				continue
			}
			b.Set(key, session)
			connectSpan.End()
			atomic.AddUint64(&app.PromMetrics.SshConnectionsTotal, 1)
			ownedSessions.Set(key, session)
			// 启动应用层保活：终端空闲、日志采集长跑期间都要能尽早发现半开连接。
			session.StartKeepAlive(sshx.SshKeepAliveInterval())
			// 本连接的键盘输入默认落到刚建立的这个会话（键含 @nodeIP）
			activeKey = key
			// 保存认证信息，供后续自动连接其他节点使用
			b.SetLastAuth(msg.CaseID, msg)
			// 归属工单并回放近期日志（异常重传）
			logpush.DefaultHub.SetCase(sub, msg.CaseID)
			models.SendMsg(ws, models.OutMessage{Type: "ssh_connected", CaseID: msg.CaseID, CustomUI: cui})
			// 会话建立事件与会话销毁事件成对出现：只有销毁没有建立时，
			// 无法区分「会话从没建过」和「建了又被清掉」——这两种处置完全不同。
			app.LogSessionCreated(session, key, connID, role, "ssh_connect")
			logpush.Blog("INFO", "ssh.connected", "SSH 认证成功", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"user": msg.Username, "host": msg.Host, "port": msg.Port, "key": key,
				"conn_id": connID, "role": role,
			})
			// 抗崩溃（收口设计 §3.2）：bridge 重启后 spc 终态还原的 watcher 没了，
			// 凭持久意图账本把原件放回去。放进协程：集合性还原可能几十秒，
			// 不能阻塞连接回包；无意图时几乎零开销。
			go logcollect.RecoverSpcRestoreIntents(b, session, msg.TraceID, msg.CaseID, msg.NodeIP, cui)

			caseID := msg.CaseID
			terminal.On_output_start(session, ws, stdout, caseID, func(reason string) {
				if reason == sshx.SessionReasonRemoteEOF {
					b.DestroySession(key, sshx.SessionReasonRemoteEOF, "")
					ownedSessions.Remove(key)
					return
				}
				b.Remove(key)
				ownedSessions.Remove(key)
			})

		case "ssh_input":
			s, key := b.ResolveInputSession(msg, activeKey, ownedSessions)
			if s == nil {
				// 以前这里查不到会话就静默返回，表现为"字打进去了、什么都不到、日志也干净"。
				// 现在必须留痕：先入库，再（节流）提示前端，避免再次无声无息。
				if lastInputRejectAt.IsZero() || time.Since(lastInputRejectAt) >= app.InputRejectNoticeInterval {
					lastInputRejectAt = time.Now()
					logpush.Blog("ERROR", "ssh_input.session_missing", "SSH 会话不存在，键盘输入已丢弃", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
						"input_type": "ssh_input", "active_key": activeKey,
						"exact_key": app.SessionKey(msg.CaseID, msg.NodeIP), "data_len": len(msg.Data),
					})
					models.SendMsg(ws, models.OutMessage{
						Type:    "ssh_input_rejected",
						CaseID:  msg.CaseID,
						Message: "SSH 会话不存在，输入未送达远端（请重新连接终端）",
					})
				}
				continue
			}
			if key != app.SessionKey(msg.CaseID, "") && key != app.SessionKey(msg.CaseID, msg.NodeIP) {
				log.Printf("[Bridge] ssh_input 兜底命中会话: case=%s key=%s active=%s", msg.CaseID, key, activeKey)
			}
			// 键盘输入本身就是最强的使用证据：用户刚敲过键，shell 被 TMOUT 踢掉时
			// 必须重建而不是判"已断开"，否则"敲完命令去查资料"必然换来一次重连。
			s.MarkBusy()
			s.Send(msg.Data)

		case "ssh_inject_command":
			s, key := b.ResolveInputSession(msg, activeKey, ownedSessions)
			if s == nil {
				if lastInputRejectAt.IsZero() || time.Since(lastInputRejectAt) >= app.InputRejectNoticeInterval {
					lastInputRejectAt = time.Now()
					logpush.Blog("ERROR", "ssh_input.session_missing", "SSH 会话不存在，命令注入已丢弃", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
						"input_type": "ssh_inject_command", "active_key": activeKey,
						"exact_key": app.SessionKey(msg.CaseID, msg.NodeIP),
					})
					models.SendMsg(ws, models.OutMessage{
						Type:    "ssh_input_rejected",
						CaseID:  msg.CaseID,
						Message: "SSH 会话不存在，命令未送达远端（请重新连接终端）",
					})
				}
				continue
			}
			if key != app.SessionKey(msg.CaseID, "") && key != app.SessionKey(msg.CaseID, msg.NodeIP) {
				log.Printf("[Bridge] ssh_inject_command 兜底命中会话: case=%s key=%s active=%s", msg.CaseID, key, activeKey)
			}
			s.InjectCommand(msg.Command)

		case "resume":
			// P0-2: 浏览器重连时触发历史日志回放
			if msg.CaseID != "" {
				logpush.DefaultHub.SetCase(sub, msg.CaseID)
				models.SendMsg(ws, models.OutMessage{Type: "resumed", CaseID: msg.CaseID, TraceID: msg.TraceID, CustomUI: cui})
				log.Printf("[Bridge] 收到 resume 信号: case=%s 触发日志回放", msg.CaseID)
			}

		case "pong":
			// WebSocket 保活心跳响应：客户端回复 pong，连接保持活跃
			// 不需要特殊处理，只需确保消息被识别即可

		case "ssh_disconnect":
			// 关闭当前 WebSocket 所拥有的该工单全部会话，防止自动节点会话泄漏。
			for key, owned := range ownedSessions.DrainCase(msg.CaseID) {
				if current := b.Get(key); current == owned {
					// 显式断开与"页面被关掉"在日志里必须区分：前者是用户意图，
					// 后者是事故；两者都走同一入口，靠 reason 区分。
					b.DestroySession(key, sshx.SessionReasonClientDisconnect, connID)
				}
			}
			models.SendMsg(ws, models.OutMessage{Type: "ssh_disconnected", CaseID: msg.CaseID})

		case "ssh_exec_command":
			// 根据 nodeIP 路由到正确的 SSH 会话
			s, key := b.ResolveSession(msg)
			if s == nil && msg.NodeIP != "" {
				// 自动连接新节点
				s = b.AutoConnectNode(ws, msg, ownedSessions, connID, role)
				key = app.SessionKey(msg.CaseID, msg.NodeIP)
			}
			// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话
			if s == nil && sub.CaseID != "" {
				if fb := b.Get(app.SessionKey(sub.CaseID, "")); fb != nil {
					s = fb
					key = app.SessionKey(sub.CaseID, "")
					log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", sub.CaseID, key)
					// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
					logpush.Blog("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, sub.CaseID, msg.NodeIP, cui, map[string]any{
						"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
					})
				}
			}
			if s == nil {
				logpush.Blog("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "key": app.SessionKey(msg.CaseID, msg.NodeIP),
					"sub_case_id": sub.CaseID, "has_fallback_target": sub.CaseID != "",
				})
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.ExecID == "" {
				// 参数缺失属"命令被拒绝"：此前只回消息不打日志，云端完全无痕。
				logpush.Blog("ERROR", "exec.rejected", "命令被拒绝：缺少 exec_id 参数", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"error_type": "missing_exec_id", "channel": "marker", "node_ip": msg.NodeIP,
					"container": msg.Container, "cmd_len": len(msg.Command),
				})
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "缺少 exec_id 参数", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.Command == "" {
				logpush.Blog("ERROR", "exec.rejected", "命令被拒绝：缺少 command 参数", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"error_type": "missing_command", "channel": "marker", "exec_id": msg.ExecID,
					"node_ip": msg.NodeIP, "container": msg.Container,
				})
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: "缺少 command 参数", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}

			// 包装容器命令
			wrappedCmd := sshx.WrapContainerCommand(msg.Command, msg.Container, s.Simulation)
			logpush.Blog("INFO", "exec.start", "开始执行命令", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"exec_id": msg.ExecID, "key": key, "container": msg.Container, "cmd_len": len(wrappedCmd),
			})
			log.Printf("[Bridge] EXEC_START: key=%s node=%s container=%s exec_id=%s cmd_len=%d",
				key, msg.NodeIP, msg.Container, msg.ExecID, len(wrappedCmd))
			log.Printf("[Bridge] EXEC_CMD: sha256=%s redacted=%q", logpush.CommandSHA256(wrappedCmd), logpush.RedactCommand(wrappedCmd))

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
				logpush.Blog("INFO", "exec.output", "命令执行输出", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id":          msg.ExecID,
					"exit_code":        exitCode,
					"timeout":          result.Timeout,
					"error_type":       errType,
					"output_len":       outLen,
					"output_sha256":    logpush.CommandSHA256(output),
					"command_redacted": logpush.RedactCommand(wrappedCmd),
					"command_sha256":   logpush.CommandSHA256(wrappedCmd),
				})

				logpush.Blog(logLevel, "exec.done", "命令执行完成", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "exit_code": exitCode, "timeout": result.Timeout,
					"error_type": errType, "output_len": outLen,
				})
				log.Printf("[Bridge] EXEC_DONE: exec_id=%s node=%s %s error_type=%s output_len=%d",
					msg.ExecID, msg.NodeIP, exitInfo, errType, outLen)
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Output: output, ExitCode: exitCode, ErrorType: errType,
					TraceID: msg.TraceID, CustomUI: cui,
				})
			}()

		case "ssh_exec_process":
			// 会话路由与 log_collect/acli_sync 对齐：custom-ui 登录建会话时用的
			// case_id 可能是工单号或 terminal-only，管理端下发只需要按 node_ip
			// 复用同一 bridge 进程里已存在的 SSH 会话，不再新建连接。
			s, key, _ := logcollect.ResolveLogCollectSession(b, msg)
			if s == nil && msg.NodeIP != "" {
				s = b.AutoConnectNode(ws, msg, ownedSessions, connID, role)
				key = app.SessionKey(msg.CaseID, msg.NodeIP)
			}
			// 防御（A 修复）：exec 消息缺少 case_id/node_ip 时，回退到当前连接归属的会话。
			// 因为一次 WebSocket 连接在 ssh_connect 后只存在一个会话，可据此兜底，
			// 避免空 case_id 导致 exec.session_missing、回采失败且无 DB 记录。
			if s == nil && sub.CaseID != "" {
				if fb := b.Get(app.SessionKey(sub.CaseID, "")); fb != nil {
					s = fb
					key = app.SessionKey(sub.CaseID, "")
					log.Printf("[Bridge] EXEC 回退到连接归属会话: case=%s key=%s", sub.CaseID, key)
					// 结构化回采：记录兜底发生（原始 case_id 可能为空，已回退到连接归属工单）
					logpush.Blog("INFO", "exec.session_fallback", "EXEC 回退到连接归属会话(空 case_id 兜底)", msg.TraceID, sub.CaseID, msg.NodeIP, cui, map[string]any{
						"original_case_id": msg.CaseID, "key": key, "exec_id": msg.ExecID,
					})
				}
			}
			if s == nil {
				logpush.Blog("ERROR", "exec.session_missing", "SSH 会话不存在，无法执行命令(隔离通道)", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "key": app.SessionKey(msg.CaseID, msg.NodeIP),
					"sub_case_id": sub.CaseID, "has_fallback_target": sub.CaseID != "",
				})
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "SSH 会话不存在（需先 ssh_connect）", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.ExecID == "" {
				logpush.Blog("ERROR", "exec.rejected", "命令被拒绝：缺少 exec_id 参数", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"error_type": "missing_exec_id", "channel": "isolated", "node_ip": msg.NodeIP,
					"container": msg.Container, "cmd_len": len(msg.Command),
				})
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "缺少 exec_id 参数", ExitCode: -1,
					TraceID: msg.TraceID, CustomUI: cui,
				})
				continue
			}
			if msg.Command == "" {
				logpush.Blog("ERROR", "exec.rejected", "命令被拒绝：缺少 command 参数", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"error_type": "missing_command", "channel": "isolated", "exec_id": msg.ExecID,
					"node_ip": msg.NodeIP, "container": msg.Container,
				})
				models.SendMsg(ws, models.OutMessage{
					Type: "exec_result", CaseID: msg.CaseID, ExecID: msg.ExecID,
					Stderr: "缺少 command 参数", ExitCode: -1,
				})
				continue
			}

			// 包装容器命令
			wrappedCmd := sshx.WrapContainerCommand(msg.Command, msg.Container, s.Simulation)
			log.Printf("[Bridge] EXEC_ISOLATED_START: key=%s node=%s container=%s exec_id=%s cmd_len=%d",
				key, msg.NodeIP, msg.Container, msg.ExecID, len(wrappedCmd))
			commandHash := logpush.CommandSHA256(wrappedCmd)

			// P0: 记录命令发起（包含 trace_id）
			logpush.Blog("INFO", "exec.request", "收到命令执行请求", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"exec_id":          msg.ExecID,
				"command_redacted": logpush.RedactCommand(wrappedCmd),
				"command_sha256":   commandHash,
				"container":        msg.Container,
				"trace_id":         msg.TraceID,
			})

			atomic.AddUint64(&app.PromMetrics.ExecCommandsTotal, 1)
			receiveCtx, receiveSpan := otel.Tracer("terminal_bridge").Start(
				logpush.ContextFromMessage(msg), "terminal_bridge.websocket.receive",
				trace.WithAttributes(attribute.String("exec.id", msg.ExecID), attribute.String("message.type", msg.Type)),
			)
			receiveSpan.End()
			req := models.ExecRequestContext{
				Context: receiveCtx, CaseID: msg.CaseID, ConversationID: msg.ConversationID,
				ExecID: msg.ExecID, ToolCallID: msg.ToolCallID, TraceID: msg.TraceID,
				Traceparent: msg.Traceparent, Tracestate: msg.Tracestate, TestRunID: msg.TestRunID, NodeIP: msg.NodeIP,
				Container: msg.Container, CustomUI: cui, Command: wrappedCmd,
				CommandRedacted: logpush.RedactCommand(wrappedCmd), CommandSHA256: commandHash,
				Pty: msg.Pty,
			}
			var requestedTimeout time.Duration
			if msg.Timeout > 0 {
				requestedTimeout = terminal.CommandTimeout(msg.Timeout)
			}
			go terminal.ExecCommandIsolated(s, ws, req, requestedTimeout, msg.OutputFilters)

		case "vm_console_op":
			// 虚拟机控制台截图固定操作通道：入口即校验，任何非法输入都不触发命令执行。
			// 刻意不复用 sub.CaseID 兜底会话——截图必须精确落在 node_ip 指定的宿主机上，
			// 找不到目标会话时 fail-closed，绝不把截图命令发到错误节点。
			vmReq, errType := terminal.ParseVMConsoleRequest(msg, cui)
			if errType != "" {
				logpush.Blog("ERROR", "vm_console.rejected", "vm_console_op 入口校验失败，已拒绝执行", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "operation": msg.Operation, "capture_id": msg.CaptureID, "error_type": errType,
				})
				rejected := terminal.VmConsoleResultMessage(vmReq, -1, msg.TraceID, "", 0)
				rejected.TraceID = msg.TraceID
				rejected.ErrorType = errType
				models.SendMsg(ws, rejected)
				continue
			}

			s, key := b.ResolveSession(msg)
			if s == nil && msg.NodeIP != "" {
				// 复用既有自动连接：使用最近一次 ssh_connect 的认证信息连接目标节点。
				s = b.AutoConnectNode(ws, msg, ownedSessions, connID, role)
				key = app.SessionKey(msg.CaseID, msg.NodeIP)
			}
			if s == nil {
				atomic.AddUint64(&app.PromMetrics.VmConsoleOpErrors, 1)
				logpush.Blog("ERROR", "vm_console.session_missing", "SSH 会话不存在，无法执行控制台固定操作", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"exec_id": msg.ExecID, "operation": msg.Operation, "key": app.SessionKey(msg.CaseID, msg.NodeIP),
				})
				missing := terminal.VmConsoleResultMessage(vmReq, -1, msg.TraceID, "", 0)
				missing.TraceID = msg.TraceID
				missing.ErrorType = terminal.VmConsoleErrSessionMissing
				models.SendMsg(ws, missing)
				continue
			}

			logpush.Blog("INFO", "vm_console.request", "收到虚拟机控制台固定操作请求", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"exec_id": vmReq.ExecID, "operation": vmReq.Operation, "capture_id": vmReq.CaptureID,
				"host_node_id": vmReq.HostNodeID, "vm_id": vmReq.VMID, "key": key,
				"timeout_seconds": vmReq.TimeoutSeconds,
			})
			atomic.AddUint64(&app.PromMetrics.VmConsoleOpsTotal, 1)
			go terminal.RunVMConsoleOp(s, func(result models.OutMessage) { models.SendMsg(ws, result) }, vmReq)

		case "acli_sync":
			go acli.HandleAcliSync(b, ws, msg, cui)

		case "list_sessions":
			// 管理端枚举「custom-ui + bridge 已登录」的环境；只回会话摘要，不回凭据。
			logpush.Blog("INFO", "session.list", "管理端查询已登录 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{})
			models.SendMsg(ws, models.OutMessage{
				Type: "list_sessions_result", CaseID: msg.CaseID,
				Sessions: b.ListSessions(), TraceID: msg.TraceID, CustomUI: cui,
			})

		case "bridge_log_token":
			// 前端下发的直传凭据：拿到即启动后台 worker，此后桥侧日志不再依赖浏览器。
			// 令牌为空/endpoint 非法时 configure 直接返回，前端保持原有的浏览器回采链路。
			logpush.LogUpload.Configure(msg.LogToken, msg.LogEndpoint, msg.CaseID, logpush.DefaultHub.InstanceID)
			models.SendMsg(ws, models.OutMessage{Type: "bridge_log_token_ack", CaseID: msg.CaseID, Status: "ok"})
			logpush.Blog("INFO", "log_upload.token_received", "收到桥日志直传凭据", msg.TraceID, msg.CaseID, "", cui, map[string]any{
				"endpoint":     msg.LogEndpoint,
				"token_length": len(msg.LogToken),
			})

		case "delivery_submit":
			// 交付作业化：立刻返回 job_id，执行权交给 bridge，前端只轮询进度。
			go handleDeliverySubmit(b, ws, msg, cui)

		case "delivery_status":
			go handleDeliveryStatus(b, ws, msg, cui)

		case "delivery_resume":
			go handleDeliveryResume(b, ws, msg, cui)

		case "delivery_cancel":
			go handleDeliveryCancel(b, ws, msg, cui)

		case "log_collect":
			go logcollect.HandleLogCollect(b, ws, msg, cui)

		case "sftp_op":
			// 下载在独立 goroutine 里跑：一个 5 GiB 的流不能把交互式 shell 的消息循环堵住。
			go handleSftpOp(b, ws, msg, cui, connID)
		}
	}
}

// ── 主入口 ────────────────────────────────────────────────────────────────────
