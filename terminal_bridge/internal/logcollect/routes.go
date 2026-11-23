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
//
// 会话自愈回调必须传进去：采集期间 SSH 会话可能因 TMOUT/网络/节点重启消失，
// 现场表现就是一次"SSH 会话未连接"；bridge 已持有 lastAuth，能自己重建。
func handleCollectRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleLogCollect(ctx.Hub, ctx.WS, msg, ctx.CUI, ctx.Life.ReconnectSession)
}
