package logcollect

// 日志收集执行通道（acli log_collect）。
//
// 第一性原理：
//   1. 云端服务永远无法直连客户私网 HCI，执行只能发生在 bridge 的 SSH 会话内；
//   2. acli 插件命令在容器内 exec 时要求 stdin 为 tty，必须走 PTY（见 pty 改造）；
//   3. 采集是异步长任务，create 只负责下发；进度由 progress 轮询，且进度只反映
//      "发起节点"的本地视图，因此调用方必须把请求路由回同一节点；
//   4. 插件本身不做参数防注入（-s / -p 直接拼 shell），bridge 是最后一道防线，
//      所有外部输入必须白名单校验 + 单引号转义。

import (
	"context"
	"fmt"
	"golang.org/x/net/websocket"
	"strings"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/logcontract"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"time"
)

// ReconnectFunc 用已保存的认证重建到节点的 SSH 会话；nil 表示不启用自愈。
type ReconnectFunc func(caseID, nodeIP, connID string) (*sshx.SSHSession, error)

// HandleLogCollect 处理 log_collect 消息族。
func HandleLogCollect(b *session.Hub, ws *websocket.Conn, msg models.InMessage, cui string, reconnect ReconnectFunc) {
	// 链路 ID 先算：会话缺失这类失败同样要能按链路检索与聚合。
	traceID := strings.TrimSpace(msg.TraceID)
	if traceID == "" {
		traceID = "lc-" + firstNonEmpty(strings.TrimSpace(msg.ExecID), "unknown")
	}
	msg.TraceID = traceID

	s, key, match := b.ResolveForNode(msg)
	// 会话自愈：采集可能持续几十分钟，期间节点 sshd 重启、网络闪断、bridge 重启
	// 都会让会话消失。bridge 早已持有 lastAuth，正确做法是自己重连一次，
	// 而不是把"SSH 会话未连接"甩给用户（2026-11-23 Q2026112300730 的观感问题）。
	if s == nil && reconnect != nil && msg.CaseID != "" && msg.NodeIP != "" {
		reconnected, err := reconnect(msg.CaseID, msg.NodeIP, "log-collect-auto")
		if err == nil && reconnected != nil {
			s, key, match = reconnected, session.SessionKey(msg.CaseID, msg.NodeIP), "auto_reconnect"
			evidence.Emit("INFO", "log_collect.session_auto_reconnect", "会话已消失，已用已存认证自动重建", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"log_action": msg.LogAction, "session_key": key,
			})
		} else if err != nil {
			evidence.Emit("WARN", "log_collect.session_auto_reconnect_failed", "会话自愈失败，需人工重连", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"log_action": msg.LogAction, "error": err.Error(),
			})
		}
	}
	if s != nil {
		// 业务活跃打戳：交互 shell 有 TMOUT=600，一次采集常常跑十几分钟且期间零键盘输入，
		// shell 必然在 600s 被踢。没有这个戳，EOF 会被当成整条连接失效并 destroySession，
		// 之后每次 progress 都是 session_missing——前端只能干等到 60s 超时，
		// 报成"progress 超时（60s）"（2026-10-28 现场）。连接与 exec 通道其实都还活着。
		s.MarkBusy()
		// 钉住会话：采集/交付期间无人持有也绝不回收；任务结束后仍保留 2 小时，
		// 覆盖"用户关页面去查资料/开会，回来再看结果"这一常态。
		if _, pinned := b.PinExpiry(key); !pinned {
			if until := b.Pin(key, session.SessionPinTTL()); !until.IsZero() {
				evidence.Emit("INFO", "session.pinned", "会话已按日志收集任务钉住", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"session_key": key, "until": until.UTC().Format(time.RFC3339),
				})
			}
		} else {
			b.Pin(key, session.SessionPinTTL())
		}
	}
	// 精确命中是常态，只有回退才留痕：日志要能回答"这条命令为什么落到了这台机器"。
	if s != nil && match != "exact" {
		evidence.Emit("INFO", "log_collect.session_fallback", "日志收集回退复用 SSH 会话", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"log_action": msg.LogAction, "session_key": key, "match": match,
		})
	}
	if s == nil {
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: msg.LogAction,
			Status: "error", ErrorType: "session_missing", ErrorCode: logcontract.ErrEnvSessionMissing,
			Message: "SSH 会话不存在（需先 ssh_connect）", TraceID: traceID, CustomUI: cui,
		})
		evidence.Emit("ERROR", "log_collect.session_missing", "日志收集动作缺少 SSH 会话", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"log_action": msg.LogAction, "task_id": msg.ExecID,
			// 定性字段：会话数为 0 = 连接生命周期已结束；会话数 >0 但键对不上 = 路由/键问题。
			// 历史日志只有 action + task_id，两种情况看起来一模一样，只能靠猜。
			"requested_key":   session.SessionKey(msg.CaseID, msg.NodeIP),
			"active_sessions": b.Count(),
			"session_keys":    b.Keys(8),
		})
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), logCollectCancelTimeout)
	defer cancel()

	action := strings.TrimSpace(msg.LogAction)
	startedAt := time.Now()
	outcome := "error"
	// 终态证据采集轮次：由各分支设置，defer 统一触发。
	// 2026-11-23 全终态方案：成功/失败/取消都必须留下进程日志，不能只在成功终态采。
	collectPhase := ""
	// 统一收尾日志：任何分支（含提前 return）都会留下耗时与结果，便于远端定位。
	defer func() {
		evidence.Emit("INFO", "log_collect.done", "日志收集动作结束", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"log_action": action, "task_id": msg.ExecID, "outcome": outcome,
			"duration_ms": time.Since(startedAt).Milliseconds(),
		})
		// 终态补采：证据链路异步 + 独立超时，任何失败都不拖慢/改变主流程结果。
		// 分支未显式设置时按 action+outcome 兜底：create 成功=init、失败=terminal_failed，
		// cancel=terminal_failed；progress 只在 completed（显式设 terminal）时采。
		if collectPhase == "" {
			switch action {
			case "create":
				if outcome == "ok" {
					collectPhase = processLogPhaseInit
				} else {
					collectPhase = processLogPhaseTerminalFail
				}
			case "cancel":
				collectPhase = processLogPhaseTerminalFail
			}
		}
		if collectPhase != "" {
			if cfg, ok := EvidenceForByCase(msg.ExecID, msg.CaseID); ok {
				phase := collectPhase
				go func() {
					evidenceCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
					defer cancel()
					collectNodeLogs(evidenceCtx, s, cfg, phase)
				}()
			}
		}
	}()
	evidence.Emit("INFO", "log_collect.start", "日志收集动作开始", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"log_action": action, "task_id": msg.ExecID, "ticket_no": msg.TicketNo,
		"modules": msg.Modules, "date_from": msg.DateFrom, "date_to": msg.DateTo,
		"ips": msg.IPs, "extra_paths_count": len(msg.ExtraPaths),
		"remote_url_set":   strings.TrimSpace(msg.RemoteURL) != "",
		"evidence_enabled": strings.TrimSpace(msg.EvidenceTicket) != "",
	})

	// 证据链路只在 create 时初始化：探测异步跑，绝不拖延 acli 下发。
	if action == "create" {
		configureEvidence(msg, cui)
	}

	switch action {
	case "probe":
		logServer := strings.TrimSpace(msg.RemoteURL)
		if logServer == "" {
			logServer = logCollectDefaultLogServer
		}
		probe := probeLogCollectEnv(s, ctx, logServer)
		outcome = "ok"
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "ok", LogData: probe, TraceID: traceID, CustomUI: cui,
			TicketNo: msg.TicketNo,
		})
		evidence.Emit("INFO", "log_collect.probe", "日志收集环境探测完成", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": probe.Architecture, "acli_installed": probe.AcliInstalled,
			"acli_version": probe.AcliVersion, "acli_build_time": probe.AcliBuildTime,
			"acli_binary":    probe.AcliBinary,
			"available_mb":   probe.AvailableMB,
			"log_server_url": probe.LogServerURL, "log_server_reachable": probe.LogServerReachable,
			"http_code": probe.LogServerHTTPCode, "latency_ms": probe.LogServerLatencyMS,
			// P1 能不能带凭据，取决于节点 acli 是否支持注入上传请求头。
			// 落库是为了让"为什么这次走了 P2"在事后可回答，而不是只能靠版本号猜。
			"acli_supports_upload_header": probe.AcliSupportsUploadHeader,
			"catalog_source":              probe.CatalogSource, "modules_count": len(probe.Modules),
			"node_modules": probe.NodeModules,
			"hosts_count":  len(probe.Hosts), "catalog_note": probe.CatalogNote,
			"note": probe.Note, "key": key,
		})

	case "create":
		// ── 自定义场景（acli -c）：先证明配置写对了，再下发采集 ──────────────
		// logData 是可变参数：常规拒绝只带错误码；spc/脚本拒绝额外带逐台证据，
		// 前端在失败回包上也要把它补写进任务详情（否则证据只存在于桥日志）。
		rejectConfigWithCode := func(errType, code, reason string, logData ...map[string]any) {
			if code == "" {
				code = logcontract.ValidationErrorCode(errType)
			}
			var payload any
			if len(logData) > 0 && len(logData[0]) > 0 {
				payload = logData[0]
			}
			models.SendMsg(ws, models.OutMessage{
				Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
				Status: "error", ErrorType: errType, ErrorCode: code,
				Message: reason, TraceID: traceID, CustomUI: cui, LogData: payload,
			})
			evidence.Emit("ERROR", "log_collect.config_rejected", "自定义场景配置校验失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"error_type": errType, "error_code": code, "reason": reason, "ticket_no": msg.TicketNo,
			})
		}
		rejectConfig := func(errType, reason string) {
			rejectConfigWithCode(errType, logcontract.ValidationErrorCode(errType), reason)
		}

		// ── 前置兜底：内置插件不完整时绝不下发 ────────────────────────────────
		// 安装器 exit 0 不代表集群分发完整（Q2026112300134：3 台 plugins 空目录）。
		// 带着缺插件的节点发起采集，只会把「安装不完整」包装成下游的
		// LC-SPC-003 / LC-EXEC-xxx，把排查引向权限与磁盘。这里先查清楚。
		preflightCfg, _ := EvidenceFor(msg.ExecID)
		if missing, checked := preflightLogCollectPlugin(ctx, s, preflightCfg, msg.IPs); len(missing) > 0 {
			reportFailureSnapshot(ctx, s, preflightCfg, "plugin_incomplete")
			rejectConfigWithCode("plugin_incomplete", logcontract.ErrInstallPluginIncomplete,
				"节点内置插件不完整，继续采集必然失败："+pluginFailureDetail(missing),
				map[string]any{"plugin_missing": missing})
			return
		} else if checked {
			evidence.Emit("INFO", "log_collect.plugin_preflight_ok", "内置插件校验通过", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"task_id": msg.ExecID,
			})
		}

		var configPath string
		var configPaths []string
		var preflightMissing []string
		// ── spc 场景：收集清单是节点本地文件，必须逐台写入 ────────────────────
		// sfd_main 只下发 single_collect.py，不推送 spc；每台节点各自现场读本机
		// 那份 core.json。只写发起节点，其余节点会静默按原清单收集（缺失仅 warning），
		// 这是本功能要消灭的"看起来成功了"。
		// ── 收集脚本：先写脚本，再写场景清单 ────────────────────────────────
		// 顺序不可颠倒：spc 里的 action=script 条目指向 script/ 下的文件名，
		// 先写脚本能保证「清单落盘的那一刻，它引用的文件已经就位」，
		// 中途失败时也不会留下指向空文件的清单。
		var scriptState *spcTaskState
		if len(msg.ScriptFiles) > 0 {
			state, rejection := prepareScriptScenario(b, ctx, s, msg, traceID, cui)
			if rejection != nil {
				rejectConfigWithCode(rejection.ErrType, rejection.Code, rejection.Message)
				return
			}
			scriptState = state
			// 与 spc 同理：采集范围必须与写入集合逐台一致，不能继续用 all。
			if written := state.writtenTargets(); len(written) > 0 {
				msg.IPs = strings.Join(written, ",")
			}
		}

		var spcState *spcTaskState
		if strings.TrimSpace(msg.SpcJSON) != "" || len(msg.SpcFiles) > 0 {
			// spc 与 -c 互斥：两者都决定"收什么"，叠加执行等于后写的静默覆盖前一个。
			if strings.TrimSpace(msg.ConfigJSON) != "" {
				rejectConfig("spc_config_conflict", "spc 场景与 -c 配置不能同时下发")
				return
			}
			// 平台已把 extra_paths 合并进 spc 条目；还收到 -p 说明载荷来自旧客户端。
			if len(msg.ExtraPaths) > 0 {
				rejectConfig("spc_path_conflict", "spc 场景与额外路径不能同时下发，请升级客户端")
				return
			}
			state, rejection := prepareSpcScenario(b, ctx, s, msg, traceID, cui)
			if rejection != nil {
				// 逐台证据随拒绝一起回包：只发 ERROR 事件时，页面永远看不到
				// 哪台哪个槽位、备份命令的真实 stderr（Q2026112300134）。
				reportFailureSnapshot(ctx, s, preflightCfg, "spc_"+rejection.Code)
				// 带规范码回包：前端按码给出检查/修复建议，不再只显示"日志收集失败"。
				rejectConfigWithCode(rejection.ErrType, rejection.Code, rejection.Message, map[string]any{
					"spc_slot": rejection.Slot, "spc_slots": rejection.Slots,
					"spc_hosts": rejection.Hosts, "restore_status": rejection.RestoreStatus,
				})
				// 脚本已经写进去了但清单没通过：任务不会建立，脚本必须立刻还原，
				// 否则节点会长期跑一份"平台写了但平台不承认"的脚本。
				if scriptState != nil {
					go restoreScriptTask(b, s, scriptState, "spc_rejected", traceID, msg.CaseID, msg.NodeIP, cui)
				}
				return
			}
			spcState = state
			// 下发范围必须与写入集合逐台一致：继续用 all 会把"本轮不可达而未写入"的
			// 节点也拉进来，采集结果就成了"平台清单 + 原清单"的混合体。
			msg.IPs = strings.Join(state.writtenTargets(), ",")
			if len(state.Slots) > 0 {
				msg.Modules = append([]string(nil), state.Slots...)
			} else {
				msg.Modules = []string{state.Slot}
			}
		}
		if strings.TrimSpace(msg.ConfigJSON) != "" {
			// -c 与 -p 不能并存：CLI 的 -p 会覆盖 config.paths（get_param 的 CLI 优先）。
			// 平台已在服务端把 extra_paths 合并进 config；这里还收到 -p，说明调用方
			// 是旧版本或载荷被改过，继续执行只会"看起来发了其实没发"。
			if len(msg.ExtraPaths) > 0 {
				rejectConfig("config_path_conflict", "自定义场景与额外路径不能同时下发，请升级客户端")
				return
			}
			paths, parseErr := parseLogCollectConfig(msg.ConfigJSON)
			if parseErr != nil {
				rejectConfig("invalid_config", fmt.Sprintf("自定义场景配置非法：%v", parseErr))
				return
			}
			path, writeErr := writeLogCollectConfig(ctx, s, firstNonEmpty(msg.ExecID, traceID), msg.ConfigJSON, msg.ConfigSHA256)
			if writeErr != nil {
				rejectConfig("config_write_failed", fmt.Sprintf("配置文件写入/校验失败：%v", writeErr))
				return
			}
			configPath, configPaths = path, paths
			preflightMissing = preflightLogCollectPaths(ctx, s, paths)
			evidence.Emit("INFO", "log_collect.config_written", "自定义场景配置已写入节点", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"config_path": path, "config_sha256": msg.ConfigSHA256,
				"path_count": len(paths), "preflight_missing": preflightMissing,
			})
		}

		command, errType, errMsg := buildLogCollectCreateCommand(msg)
		if errType != "" {
			models.SendMsg(ws, models.OutMessage{
				Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
				Status: "error", ErrorType: errType, ErrorCode: logcontract.ValidationErrorCode(errType),
				Message: errMsg, TraceID: traceID, CustomUI: cui,
			})
			evidence.Emit("ERROR", "log_collect.rejected", "日志收集参数校验失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"error_type": errType, "reason": errMsg, "ticket_no": msg.TicketNo,
			})
			// 参数被拒 = 不会有任务，也就永远等不到"终态恢复"：写下去的平台清单
			// 必须就地还原，否则 core 槽位会被它长期占住。回包已先发出，
			// 恢复不占用动作预算（多节点串行可能要几十秒）。
			if spcState != nil {
				go restoreSpcTask(b, s, spcState, "create_rejected", traceID, msg.CaseID, msg.NodeIP, cui)
			}
			if scriptState != nil {
				go restoreScriptTask(b, s, scriptState, "create_rejected", traceID, msg.CaseID, msg.NodeIP, cui)
			}
			return
		}
		// 参数串由 buildLogCollectCreateCommand 校验并拼装，执行前必须经统一解析前缀，
		// 与探测同源（否则非交互 PATH 缺 /sf/bin 时会 command not found）。
		// 先保住上一次的包，再开始新的采集：产物路径固定同名，新采集会直接覆盖旧包。
		// -c 追加在最后：配置文件路径已由平台签发、bridge 回读校验过，
		// 且此时 msg.ExtraPaths 必为空（否则上面已拒绝），不存在 -p 覆盖风险。
		if configPath != "" {
			command += " -c " + remote.ShellQuote(configPath)
		}
		archiveStaleLogCollectArchive(ctx, s, msg.CaseID, traceID, cui)
		// 旧包改名可能失败（权限/目录），删不掉就只能靠"基线"识别：先量一次盘上
		// 已有文件的身份，之后探测到完全一致的文件就不再算本次产物。
		rememberLogCollectArchiveBaseline(ctx, s, msg.ExecID, msg.CaseID, msg.NodeIP, traceID, cui)
		output, exitCode, err := s.RunCommandPty(ctx, logCollectAcliInvoke(command), logCollectCreateTimeout)
		status := "ok"
		code := ""
		// 失败原因优先取进程错误，其次取 acli 输出里的首个失败行。
		// RunCommandPty 会把 ssh.ExitError 归一化成 exitCode 后返回 nil，
		// 只写 errString(err) 会得到空串（2026-10-27 实测 19 条失败 create 的 error 全是 ""），
		// 平台与桥侧日志都无法定因。
		failure := errString(err)
		// 接单确认结论必须随 create 回包与事件一起落库：事后按 trace 反查时，
		// 要能回答"当时到底有没有看见节点 running"，而不只是"exit_code=0"。
		ackState := logCollectAckUnknown
		var ackStates []string
		if err != nil || exitCode != 0 {
			status = "error"
			code = logcontract.ExecErrorCode(err, exitCode)
			failure = firstNonEmpty(failure, logCollectFailureLine(output))
			// 优先按节点明确业务拒绝文案映射专属码（磁盘不足/节点离线/日期越界/专项缺失），
			// 避免把明确的可执行根因压成泛化 LC-EXEC-003。
			if detCode, detLine, matched := detectLogCollectCreateOutputError(output); matched {
				code = detCode
				failure = firstNonEmpty(detLine, failure)
			} else if logCollectModuleMissing(output) {
				code = logcontract.ErrPlugModuleMissing
			}
		} else if detCode, detLine, matched := detectLogCollectCreateOutputError(output); matched {
			// 第一性原理：acli create 存在「打印明确拒绝原因却返回 exit_code=0」的缺陷
			// （2026-11-25 Q2026112500790：磁盘可用空间 5.72GB 小于 6GB 但退出码为 0）。
			// 若放行到 confirmLogCollectCreate，会读到上一轮遗留的 cluster_progress.json
			// 并误拉旧包报 LC-TRANSFER-011，彻底掩盖真因。必须在此立刻拦截并归因。
			status = "error"
			code = detCode
			failure = detLine
			reportFailureSnapshot(ctx, s, preflightCfg, "create_output_"+detCode)
		} else {
			// 第一性原理：退出码不是"任务已建立"的证据。必须正向确认一次。
			ack := confirmLogCollectCreate(ctx, logCollectCreateAckInterval, func(ackCtx context.Context) (string, int, error) {
				out, ec, runErr := s.RunCommandPty(ackCtx, logCollectAcliInvoke("log_collect progress"), logCollectProgressTimeout)
				return out, ec, runErr
			})
			ackState, ackStates = ack.State, ack.States
			evidence.Emit("INFO", "log_collect.create_ack", "下发后节点接单确认", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"ticket_no": msg.TicketNo, "result": ackState, "node_states": ackStates,
			})
			switch ackState {
			case logCollectAckConfirmed:
				outcome = "ok"
			case logCollectAckNotEstablished:
				status = "error"
				reportFailureSnapshot(ctx, s, preflightCfg, "create_not_established")
				// 用 015 而不是 008：008 是"查询了一个不存在的任务"，本场景是
				// "下发之后节点根本没建立"，处置动作不同（核对 acli 版本/专项 vs 核对任务 ID）。
				code = logcontract.ErrPlugCreateNotEstablished
				failure = "任务下发后节点未建立采集任务（acli 退出码为 0，但 progress 持续报告任务不存在）；请核对节点 acli 版本与所选专项，完整节点输出见任务详情"
			default:
				// 结果未知：不替节点下失败结论，保持 ok 并留痕，由前端有界等待兜底。
				outcome = "ok"
				evidence.Emit("WARN", "log_collect.create_ack_unknown", "下发后未能确认节点接单", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"ticket_no": msg.TicketNo, "node_states": ackStates,
				})
			}
		}
		// 配置文件路径与预检结论必须随 create 结果回给前端落库：
		// 只留在桥日志里，换端/关页面后就无法回答"这次到底要收什么"。
		// spc 的写入证据必须随 create 回包落库：涉及逐台改节点文件，
		// 事后要能回答"哪几台写了、备份叫什么、现在还原了没有"，只靠桥日志不可查。
		// 快照只算一次：回包与事件日志必须引用同一份数据，否则两边对不上。
		var spcSlotLabel string
		var spcSlotLabels []string
		var spcHostsTrail []map[string]any
		if spcState != nil {
			spcSlotLabel = spcState.Slot
			spcSlotLabels = append([]string(nil), spcState.Slots...)
			spcHostsTrail = spcPersistHosts(spcState.hostsCopy())
		}
		createLogData := map[string]any{
			"create_ack":        ackState,
			"create_ack_states": ackStates,
		}
		if configPath != "" {
			createLogData["config_path"] = configPath
			createLogData["config_sha256"] = msg.ConfigSHA256
			createLogData["path_count"] = len(configPaths)
			createLogData["preflight_missing"] = preflightMissing
		}
		if spcState != nil {
			createLogData["spc_slot"] = spcSlotLabel
			createLogData["spc_slots"] = spcSlotLabels
			if len(msg.SpcFiles) > 0 {
				// 多槽位没有单份原文：用「槽位:摘要」拼出本次下发的整体指纹，
				// 前端按槽位落库后可与各模块清单逐一比对。
				parts := make([]string, 0, len(msg.SpcFiles))
				for _, file := range msg.SpcFiles {
					parts = append(parts, strings.TrimSpace(file.Slot)+":"+strings.ToLower(strings.TrimSpace(file.SHA256)))
				}
				createLogData["spc_sha256"] = spcSHA256(strings.Join(parts, "|"))
				createLogData["spc_files"] = parts
			} else {
				createLogData["spc_sha256"] = spcSHA256(msg.SpcJSON)
			}
			createLogData["spc_hosts"] = spcHostsTrail
			// 落库即 pending：出现不等于已还原，还原结果由后续 restore 回执改写。
			createLogData["restore_status"] = "pending"
		}
		// 脚本证据与 spc 同构：换了端/关了页面仍能回答「节点上跑的是哪一版脚本」。
		var scriptHostsTrail []map[string]any
		if scriptState != nil {
			scriptHostsTrail = spcPersistHosts(scriptState.hostsCopy())
			scriptDigests := map[string]string{}
			if len(msg.ScriptFiles) > 0 {
				for _, file := range msg.ScriptFiles {
					scriptDigests[strings.TrimSpace(file.Code)] = strings.ToLower(strings.TrimSpace(file.SHA256))
				}
			}
			createLogData["script_codes"] = scriptState.Slots
			createLogData["script_sha256"] = scriptDigests
			createLogData["script_hosts"] = scriptHostsTrail
			createLogData["script_restore_status"] = "pending"
		}
		// create 的完整输出是定位「exit_code=0 但节点没建任务」的唯一物证。
		// 旧实现只留前 512 字节预览，而 Python Traceback 的最后一行（真正的异常类型）
		// 恰好在尾部，被切掉后现场无法定因（2026-11-09 Q2026110901673）。
		// 头尾保留 + 票据脱敏：既留住尾部异常，也不能把 Bearer 写进日志和 DB。
		// 输出上限 64KiB，避免异常刷屏把日志与 WS 报文变成无界存储。
		safeOutput := stripAnsi(redactUploadTicket(evidence.RedactSensitiveText(output), msg.UploadTicket))
		createEvidence := remote.FormatOutputPreview(safeOutput, 48*1024, 16*1024)
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, Raw: createEvidence, ErrorCode: code,
			// Message 只做短摘要：完整证据走 Raw，避免把 64KiB 原文塞进异常与提示。
			Message: firstNonEmpty(failure, strings.TrimSpace(sshx.TruncateString(safeOutput, 500))),
			Detail:  evidence.RedactCommand(redactUploadTicket("acli "+command, msg.UploadTicket)), TraceID: traceID, CustomUI: cui,
			TicketNo: msg.TicketNo, LogData: createLogData,
		})
		evidence.Emit("INFO", "log_collect.create", "日志收集任务下发", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"ticket_no": msg.TicketNo, "exit_code": exitCode, "error": failure,
			"command":                 evidence.RedactCommand(redactUploadTicket("acli "+command, msg.UploadTicket)),
			"p1_ticket_used":          msg.UploadTicket != "",
			"config_used":             configPath != "",
			"config_path":             configPath,
			"path_count":              len(configPaths),
			"preflight_missing_count": len(preflightMissing),
			"create_ack":              ackState,
			"create_ack_states":       ackStates,
			"output_len":              len(safeOutput),
			"output_truncated":        len(safeOutput) > 48*1024+16*1024,
			// 同一条链路 ID 贯穿 create 事件、WS 回包与任务 detail，事后可按 trace 反查全部证据。
			"output_preview": createEvidence,
			"spc_used":       spcState != nil,
			"spc_slot":       spcSlotLabel,
			"spc_hosts":      spcHostsTrail,
			"script_used":    scriptState != nil,
			"script_codes":   scriptUsedCodes(scriptState),
			"script_hosts":   scriptHostsTrail,
		})
		if spcState != nil {
			if status != "ok" {
				// 任务没建立就没有终态可等：立刻还原，别让平台清单占着槽位。
				// 回包已发出，恢复是集合性串行操作，放进协程不占动作预算。
				go restoreSpcTask(b, s, spcState, "create_failed", traceID, msg.CaseID, msg.NodeIP, cui)
			} else {
				// 终态恢复交给常驻进程：平台侧轮询只在页面打开时发生，
				// 操作员一关页面就没人收尾，节点会一直挂着平台清单。
				watchSpcRestore(b, s, spcState, traceID, msg.CaseID, msg.NodeIP, cui)
			}
		}
		if scriptState != nil {
			if status != "ok" {
				go restoreScriptTask(b, s, scriptState, "create_failed", traceID, msg.CaseID, msg.NodeIP, cui)
			} else {
				watchScriptRestore(b, s, scriptState, traceID, msg.CaseID, msg.NodeIP, cui)
			}
		}

	case "progress":
		output, exitCode, err := s.RunCommandPty(ctx, logCollectAcliInvoke("log_collect progress"), logCollectProgressTimeout)
		progress := ParseLogCollectProgress(output)
		progress.NodeState = classifyLogCollectNodeState(progress)
		// 这里刻意**不用** stat 结果判断"完成归属"：P1 直传成功后插件会自删压缩包，
		// mtime 在成功路径上必然缺失，拿它当归属证据只会把真完成判成假完成。
		// 归属改由前端比对"下发前锚定的报告指纹"来判断，不依赖任何文件事实。
		//
		// 但压缩包大小没有别的来源：插件 progress 不打印大小，平台侧只有 P2 分片合并时
		// 才知道字节数，P1 直传的记录因此长期显示「未记录」。这里只补一个纯信息字段，
		// 量不到就留 0，不参与任何状态判断。
		//
		// 路径不能等插件施舍（见 fallbackLogCollectArchivePath）：插件只在 100% 完成后
		// 才打印真实路径，那时包已自删。插件给了就用插件的，没给就按约定推导。
		probePath := progress.ArchivePath
		// 来源必须与路径一起记录：插件的路径是"报告声称"，推导路径是"约定假设"，
		// 两者出问题时的排查方向完全不同（插件改目录 vs 约定失效）。
		probeSource := "plugin"
		if strings.TrimSpace(probePath) == "" {
			probePath = fallbackLogCollectArchivePath(msg.CaseID)
			probeSource = "fallback"
		}
		if strings.TrimSpace(probePath) == "" {
			probeSource = "none"
		}
		// 刻意不回填 progress.ArchivePath：前端 decideNodeSync 用「节点无任务 + 有 archive_path」
		// 判定任务已完成，回填一个未经验证的推导路径会让它在"任务被清理但包还在"时误判完成。
		// 大小是纯展示字段，不参与状态机，因此只补 size。
		// 诊断信息跟着进度事件一起落库：量不到时必须留下"为什么量不到"。
		archiveProbeResult := ProbeLogCollectArchive(ctx, sshx.NewSSHRunner(s), probePath)
		progress.ArchiveSize = archiveProbeResult.Size
		// 存在性必须跟着进度一起回给前端：判断"能不能补交付"只认这个结论，
		// 不认报告里的 archive_path（那个字段在包被删后仍然存在）。
		// ProbeLogCollectArchive 返回三态指针：探测失败时保持 nil，前端据此显示
		// 「无法确认」而不是把未知当成"已删除"。
		progress.ArchiveExists = archiveProbeResult.Exists
		// 固定路径上的包也可能是同工单上一批次残留下来的：它的字节数与 identity
		// 从本次下发之前就没变过，不能当成本次采集的体积（Q2026112700769）。
		if progress.ArchiveSize > 0 &&
			logCollectArchiveIsResidual(ctx, s, msg.ExecID, probePath, progress.ArchiveSize) {
			residualSize := progress.ArchiveSize
			progress.ArchiveSize = 0
			progress.ArchiveResidual = true
			if markArchiveResidualNotified(msg.ExecID) {
				evidence.Emit("WARN", "log_collect.archive_residual", "压缩包是同工单上一批次残留", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"task_id": msg.ExecID, "path": probePath, "size": residualSize,
					"ticket_no": msg.TicketNo,
				})
			}
		}
		// 采集终态只补采一次证据：节点侧 zip 中央目录 + 采集进程日志。
		// 异步 + 独立超时：证据链路的任何失败都不允许拖慢/改变进度回包。
		// 逐台明细是 acli 进度原文里已解析好的事实，只落桥日志即可供管理端回读；
		// 无需证据票据，也不依赖数据面端点（那类端点只承载需要票据的四类证据）。
		if progress.NodeState == "completed" && len(progress.Hosts) > 0 && markHostResultsReported(msg.ExecID) {
			evidence.Emit("INFO", "log_collect.host_results", "日志收集逐台明细", traceID, msg.CaseID, msg.NodeIP, cui,
				logCollectHostResultExtra(progress.Hosts))
		}
		if progress.NodeState == "completed" {
			// 终态 zip 中央目录探测仍在此处（有 probePath/ArchiveSize 局部变量）；
			// 进程日志采集统一移到 defer 终态钩子，避免只在成功门触发、失败路径零证据。
			if evidenceCfg, ok := EvidenceForByCase(msg.ExecID, msg.CaseID); ok {
				session, evidencePath, evidenceSize := s, probePath, progress.ArchiveSize
				go func() {
					evidenceCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
					defer cancel()
					inspectNodeArchive(evidenceCtx, session, evidenceCfg, evidencePath, evidenceSize)
				}()
			}
			collectPhase = processLogPhaseTerminal
		}
		status := "ok"
		code := ""
		if err != nil || exitCode != 0 {
			status = "error"
			code = logcontract.ExecErrorCode(err, exitCode)
		} else {
			outcome = "ok"
			if !progress.ParseOK {
				// 解析降级不是失败：单独出码，前端展示原文，任务状态仍为进行中。
				code = logcontract.ErrPlugProgressDegr
			}
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, LogData: progress, Raw: output, ErrorCode: code,
			Message: errString(err), TraceID: traceID, CustomUI: cui,
			TicketNo: msg.TicketNo,
		})
		// 探测结论必须随进度一起落库：现场问"为什么面板不给上传入口"时，
		// 只有这三个字段能回答"探的是哪条路径、包到底在不在、为什么量不到"。
		// 以前只回给前端、不写日志 —— Q2026102701403 现场库里就查不到 archive_exists，
		// 只能靠 archive_size 有值反推 exists=true，多花了一轮定位。
		fields := map[string]any{
			"parse_ok": progress.ParseOK, "percent": progress.Percent,
			"done": progress.Done, "total": progress.Total,
			"status_text": progress.Status, "archive_path": progress.ArchivePath,
			"upload_url": progress.UploadURL, "upload_state": progress.UploadState,
			// 上传失败必须留下能定因的东西：只有"失败"两个字时，平台与桥侧
			// 都只能去 5888 翻服务端日志才能判断是 401、超时还是网络。
			"upload_error": progress.UploadError, "upload_raw_tail": progress.UploadRawTail,
			"node_state": progress.NodeState,
			// total_time 是 acli 唯一的自证耗时字段，也是平台侧判定"这份报告属于哪次下发"
			// 的独立证据：任务只存在 3 秒、报告却自称跑了 3 分 38 秒，必是上一次任务的报告
			// （2026-11-06 Q2026110666667）。它以前只在回包里给前端、不落库，事后无法复核。
			"total_time":  progress.TotalTime,
			"hosts_count": len(progress.Hosts), "archive_size": progress.ArchiveSize,
			"archive_exists": progress.ArchiveExists, "archive_probe_path": probePath,
			"archive_probe_source": probeSource, "archive_probe_level": archiveProbeResult.Level,
			// residual=盘上那份包在本次下发前就存在且此后未被写过（不是本次产物）：
			// 体积已被清零，这个布尔是"为什么没有体积"的唯一证据。
			"archive_residual": progress.ArchiveResidual,
			// ticket_no 必须落事件：acli 是节点级单任务，同一节点可能同时挂着
			// 别的工单的采集，事后只能靠它判断这份进度属于谁。
			"ticket_no": msg.TicketNo, "task_id": msg.ExecID,
			"exit_code": exitCode,
			"error":     errString(err), "raw_len": len(output),
		}
		// diag 只在探测失败时非空：空值不写，避免每条进度都多一个无意义键。
		// 走兜底成功时 diag 里仍保留失败那一级的 exit/stderr（形如 stat:exit=127），
		// 这是「就地止血但保留根因」的关键：Q2026102900165 现场正因为兜底成功
		// 把 diag 清空，事后完全看不出 stat 从来跑不起来。
		if archiveProbeResult.Diag != "" {
			fields["archive_probe_diag"] = archiveProbeResult.Diag
		}
		evidence.Emit("INFO", "log_collect.progress", "日志收集进度查询", traceID, msg.CaseID, msg.NodeIP, cui, fields)
		// stat 失败在采集早期是常态（包还没生成），只有进入上传窗口后的失败才值得留证：
		// 否则每次轮询都多打一条，会把真正的现场信号淹掉。
		if archiveProbeResult.Diag != "" && progress.Percent >= 80 {
			evidence.Emit("WARN", "log_collect.archive_size_probe", "压缩包大小探测降级", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"path": probePath, "level": archiveProbeResult.Level,
				"diag": archiveProbeResult.Diag,
			})
		}

	case "stat":
		// P3 编排需要先知道包大小才能 init 分片会话。
		//
		// 这里必须与 progress 共用同一套两级探测（stat → ls -l）：Q2026102900165 现场
		// stat 在 aSV 受限 shell 里 exit 127（命令不存在），包却真在盘上 29.9 MB。
		// 旧实现只有单条 stat，失败即 exists=false，于是平台对着盘上的包断言
		// 「压缩包已被插件删除」，且 Status 恒为 ok，把真诊断整个吞掉。
		// 现在结论只有三种：在（ok）/ 明确不在（missing）/ 无法确认（error）。
		path, pathErr := NormalizeArchivePath(msg.TransferSourcePath)
		if pathErr != nil {
			models.SendMsg(ws, models.OutMessage{
				Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
				Status: "error", ErrorType: "invalid_path", ErrorCode: logcontract.ErrTransferArchiveMissing,
				Message: pathErr.Error(), TraceID: traceID, CustomUI: cui,
			})
			return
		}
		probed := ProbeLogCollectArchive(ctx, sshx.NewSSHRunner(s), path)
		logData := map[string]any{
			"path": path, "size": probed.Size,
			"exists": probed.Exists, "probe_level": probed.Level,
		}
		if probed.Diag != "" {
			logData["probe_diag"] = probed.Diag
		}
		status, code, errType, message := "ok", "", "", ""
		switch {
		case probed.Exists == nil:
			// 探测本身失败：必须当失败上报，否则前端只能把"测不到"当"不存在"。
			// 再按「节点缺命令」与「通道/权限异常」分流：前者客户去放命令白名单，
			// 后者重连即可，把两者压成同一句「或」会让客户无从下手。
			status, code, errType = "error", logcontract.ErrTransferArchiveProbe, "archive_probe_unknown"
			message = "无法确认压缩包状态（节点命令不可用或通道异常）"
			if probed.Unavailable {
				code, errType = logcontract.ErrEnvProbeCommandMissing, "probe_command_missing"
				message = "无法确认压缩包状态：节点 shell 缺少探测命令（stat/ls 均不可用）"
			}
			if probed.Diag != "" {
				message += "：" + probed.Diag
			}
		case !*probed.Exists:
			// 文件不存在不是"动作失败"：它是补交付的合法结论（P1 发送成功后插件自删）。
			code = logcontract.ErrTransferArchiveMissing
			message = "节点上已确认没有该压缩包"
		default:
			outcome = "ok"
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ErrorCode: code, ErrorType: errType,
			LogData: logData, Message: message, TraceID: traceID, CustomUI: cui,
		})
		evidence.Emit("INFO", "log_collect.stat", "日志包状态探测", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"path": path, "size": probed.Size, "exists": probed.Exists,
			"probe_level": probed.Level, "probe_diag": probed.Diag,
			"status": status, "error_code": code,
		})

	case "transfer":
		// 传输可能持续数十分钟，不沿用 180s 的动作超时，单独给 2 小时预算。
		transferCtx, transferCancel := context.WithTimeout(context.Background(), logCollectTransferTimeout)
		defer transferCancel()
		if err := handleLogCollectTransfer(b, transferCtx, ws, s, msg, cui, traceID); err != nil {
			evidence.Emit("ERROR", "log_collect.transfer", "日志包交付失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"task_id": msg.ExecID, "error": err.Error(),
			})
			return
		}
		outcome = "ok"

	case "cancel":
		output, exitCode, err := s.RunCommandPty(ctx, logCollectAcliInvoke("log_collect cancel"), logCollectCancelTimeout)
		status := "ok"
		code := ""
		if err != nil || exitCode != 0 {
			status = "error"
			code = logcontract.ErrPlugCancelFailed
		} else {
			outcome = "ok"
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, Raw: output, ErrorCode: code,
			Message: firstNonEmpty(errString(err), strings.TrimSpace(output)),
			TraceID: traceID, CustomUI: cui,
		})
		evidence.Emit("WARN", "log_collect.cancel", "日志收集任务取消", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"exit_code": exitCode, "error": errString(err),
			"output_preview": stripAnsi(sshx.TruncateString(output, 512)),
		})

	case "restore_spc":
		// 平台侧兜底入口：bridge 进程被杀/重启后内存态丢失，终态恢复协程也就没了。
		// 备份名自描述（<file>.platform.<task_id>.bak），因此不需要内存也能重放恢复。
		// 双路径都幂等：备份不存在返回 skipped，绝不把"没备份"报成"已恢复"。
		logData, restoreErrType, restoreErrMsg := handleSpcRestore(b, ctx, s, msg)
		restoreStatus := "ok"
		restoreCode := ""
		if restoreErrType != "" {
			restoreStatus = "error"
			restoreCode = logcontract.ValidationErrorCode(restoreErrType)
		} else {
			outcome = "ok"
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: restoreStatus, ErrorType: restoreErrType, ErrorCode: restoreCode,
			Message: restoreErrMsg, LogData: logData, TraceID: traceID, CustomUI: cui,
		})
		evidence.Emit("INFO", "log_collect.spc_restore_requested", "平台请求恢复场景槽位", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": msg.ExecID, "spc_slot": msg.SpcSlot,
			"status": restoreStatus, "error_type": restoreErrType, "message": restoreErrMsg,
		})

	case "restore_script":
		// 平台侧兜底入口：bridge 进程被杀/重启后内存态丢失，终态恢复协程也就没了。
		// 备份名自描述（<file>.platform.<task_id>.bak），created 标记也按任务隔离，
		// 因此不需要内存也能重放恢复；备份/标记都不存在时返回 skipped。
		logData, restoreErrType, restoreErrMsg := handleScriptRestore(b, ctx, s, msg)
		restoreStatus := "ok"
		restoreCode := ""
		if restoreErrType != "" {
			restoreStatus = "error"
			restoreCode = logcontract.ValidationErrorCode(restoreErrType)
			if restoreCode == "" {
				restoreCode = logcontract.ErrScriptRestoreFailed
			}
		} else {
			outcome = "ok"
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: restoreStatus, ErrorType: restoreErrType, ErrorCode: restoreCode,
			Message: restoreErrMsg, LogData: logData, TraceID: traceID, CustomUI: cui,
		})
		evidence.Emit("INFO", "log_collect.script_restore_requested", "平台请求恢复收集脚本", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": msg.ExecID, "script_codes": msg.ScriptCodes,
			"status": restoreStatus, "error_type": restoreErrType, "message": restoreErrMsg,
		})

	case "capabilities":
		// 能力探测：旧桥对未知 action 会明确返回 invalid_action，前端据此 fail-closed，
		// 避免把 spc_files 静默忽略后按节点原清单收集（最危险的"看起来成功了"）。
		// acli_plugin_sync：本桥是否认识 acli_sync 第二阶段的 platform_url/plugin_ticket。
		// 前端当前**不**拿它当门禁（旧桥不认识新字段会直接忽略，代价只是一次白取的票据；
		// 先探测要多一次往返，还会把"探测本身失败"变成第二个静默分支），
		// 这里的职责是让排障能一眼对账：回执里没有 plugin_sync 时，先查这个位。
		outcome = "ok"
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "ok", LogData: map[string]any{
				"multi_slot_spc":       true,
				"spc_max_slots":        len(logCollectModules),
				"log_collect_evidence": true,
				"acli_plugin_sync":     true,
				// 平台据此决定是否下发 script_files：旧桥不认识会静默忽略，
				// 那会让场景引用的脚本在节点上不存在，采集直接少采。
				"platform_scripts": true,
			},
			TraceID: traceID, CustomUI: cui,
		})
		evidence.Emit("INFO", "log_collect.capabilities", "日志收集能力探测", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"multi_slot_spc": true, "spc_max_slots": len(logCollectModules),
			"log_collect_evidence": true, "acli_plugin_sync": true,
		})

	default:
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "error", ErrorType: "invalid_action", ErrorCode: logcontract.ErrPlugInvalidAction,
			Message: "log_action 仅支持 probe / create / progress / cancel / stat / transfer / restore_spc / capabilities",
			TraceID: traceID, CustomUI: cui,
		})
	}
}
