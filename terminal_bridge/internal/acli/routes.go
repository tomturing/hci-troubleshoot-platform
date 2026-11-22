package acli

import (
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/wsctx"
)

// WS 路由声明：消息类型归本域所有，装配层只做汇总登记。
// 新增或改名一条消息只改本文件，不必再翻 cmd/bridge 的装配文件（A5）。
func Routes() []wsctx.Route {
	return []wsctx.Route{
		{Type: "acli_sync", Handler: handleSyncRoute},
	}
}

// handleSyncRoute 是协议边缘：同步要跑拉包/装包/自检，必须离开派发循环，
// 否则一次 acli_sync 会把同一连接上的交互 shell 一起卡住。
func handleSyncRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleAcliSync(ctx.Hub, ctx.WS, msg, ctx.CUI)
}
