package sftp

import (
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/wsctx"
)

// WS 路由声明：消息类型归本域所有，装配层只做汇总登记（A5）。
func Routes() []wsctx.Route {
	return []wsctx.Route{
		{Type: "sftp_op", Handler: handleOpRoute},
	}
}

// handleOpRoute 是协议边缘：下载在独立 goroutine 里跑，
// 一个 5 GiB 的流不能把交互式 shell 的消息循环堵住。
func handleOpRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleSftpOp(ctx.Hub, ctx.WS, msg, ctx.CUI, ctx.ConnID)
}
