package delivery

import (
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/wsctx"
)

// WS 路由声明：消息类型归本域所有，装配层只做汇总登记（A5）。
//
// 注意本域是 PORT_ONLY（门禁规则 7）：这四个 handler 只取 ctx.WS / ctx.CUI
// 这类连接级字段，不碰 Hub/Life，传输全走注入的 NodeSession 端口，
// 因此离线测试仍可用 Fake 覆盖编排逻辑。
func Routes() []wsctx.Route {
	return []wsctx.Route{
		{Type: "delivery_submit", Handler: handleSubmitRoute},
		{Type: "delivery_status", Handler: handleStatusRoute},
		{Type: "delivery_resume", Handler: handleResumeRoute},
		{Type: "delivery_cancel", Handler: handleCancelRoute},
	}
}

// 交付作业化：立刻返回 job_id，执行权交给 bridge，前端只轮询进度。
func handleSubmitRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleDeliverySubmit(ctx.WS, msg, ctx.CUI)
}

func handleStatusRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleDeliveryStatus(ctx.WS, msg, ctx.CUI)
}

func handleResumeRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleDeliveryResume(ctx.WS, msg, ctx.CUI)
}

func handleCancelRoute(ctx *wsctx.Context, msg models.InMessage) {
	go HandleDeliveryCancel(ctx.WS, msg, ctx.CUI)
}
