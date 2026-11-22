package main

// R0c 注册式路由：WS 消息按「谁的业务谁声明」分权，装配层只做汇总与注入。
//
// 归属规则：消息类型与其 handler 写在所属域的 routes.go（导出 Routes()）。
// 本文件只负责三件事——
//  1. 注入跨域能力：会话族需要的 SPC 还原意图属采集域语义，以 SessionDeps 传入；
//  2. 汇总注册各域声明的路由，不持有任何业务分支；
//  3. 承载不属于任何业务域的桥自身消息（bridge_log_token：桥日志通道凭据）。
//
// 新增或改名一条消息不再需要改这里（A5）。

import (
	"terminal_bridge/internal/acli"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/delivery"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/sftp"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/terminal"
)

func init() {
	// 会话族：SPC 还原意图属采集域语义，由装配层注入，终端域只表达意图。
	// 这一处注入是 A5 的关键——没有它，ssh_connect 归位就会产生终端域→采集域的边。
	app.RegisterWSRoutes(terminal.SessionRoutes(terminal.SessionDeps{
		RecoverSpcRestore: logcollect.RecoverSpcRestoreIntents,
	}))

	// 已归位的域：消息类型由域自己声明（Routes()），这里只做汇总登记。
	// 新增或改名一条消息只需改对应域的 routes.go，不用再动本文件。
	app.RegisterWSRoutes(acli.Routes())
	app.RegisterWSRoutes(logcollect.Routes())
	app.RegisterWSRoutes(sftp.Routes())
	app.RegisterWSRoutes(delivery.Routes())
	app.RegisterWSRoutes(terminal.Routes())

	// 桥自身日志通道的凭据下发：不属于任何业务域，留在装配层。
	app.RegisterWSRoute("bridge_log_token", handleBridgeLogToken)
}

func handleBridgeLogToken(ctx *app.WSContext, msg models.InMessage) {
	// 前端下发的直传凭据：拿到即启动后台 worker，此后桥侧日志不再依赖浏览器。
	// 令牌为空/endpoint 非法时 configure 直接返回，前端保持原有的浏览器回采链路。
	obslog.LogUpload.Configure(msg.LogToken, msg.LogEndpoint, msg.CaseID, obslog.DefaultHub.InstanceID)
	models.SendMsg(ctx.WS, models.OutMessage{Type: "bridge_log_token_ack", CaseID: msg.CaseID, Status: "ok"})
	obslog.Emit("INFO", "log_upload.token_received", "收到桥日志直传凭据", msg.TraceID, msg.CaseID, "", ctx.CUI, map[string]any{
		"endpoint":     msg.LogEndpoint,
		"token_length": len(msg.LogToken),
	})
}
