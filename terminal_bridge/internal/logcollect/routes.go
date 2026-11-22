package logcollect

import (
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/wsctx"
)

// WS 路由声明：消息类型归本域所有，装配层只做汇总登记。
// 新增或改名一条消息只改本文件（A5）。
func Routes() []wsctx.Route {
	return []wsctx.Route{
		{Type: "log_collect", Handler: handleCollectRoute},
	}
}

// handleCollectRoute 是协议边缘：采集可能持续数十分钟，必须离开派发循环。
func handleCollectRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleLogCollect(ctx.Hub, ctx.WS, msg, ctx.CUI)
}
