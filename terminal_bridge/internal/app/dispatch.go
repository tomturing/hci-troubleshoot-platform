package app

import (
	"sync"
	"time"

	"terminal_bridge/internal/shared/models"

	"golang.org/x/net/websocket"
)

// 注册式 WS 消息路由（微内核）：app 只持有路由表与派发缝，不认识任何具体消息的
// 业务语义；各功能域在 init() 自注册 handler（R0c）。取代中央 switch 后，
// 「支持哪些消息类型」以路由表为唯一事实源，路由表守卫测试逐字冻结清单。

// LogCaseSubscriber 是日志订阅者的最小接口缝：协议边缘只需要「归属工单」读写能力，
// 不认识 obslog 的私有订阅者类型（依赖倒置，避免 app/装配层反向依赖具体实现）。
type LogCaseSubscriber interface {
	BindCase(caseID string)
	BoundCase() string
}

// WSContext 承载单条 WebSocket 连接的派发上下文：连接级状态由 handle() 信封
// 创建与维护，handler 只经此缝读写，不再依赖闭包捕获的裸局部变量。
type WSContext struct {
	B      *Bridge
	WS     *websocket.Conn
	CUI    string
	ConnID string
	Role   string
	Sub    LogCaseSubscriber
	Owned  *OwnedSessionTracker
	// ActiveKey 记录本连接最近一次 ssh_connect 建立的会话键（含 @nodeIP）。
	// 键盘输入属于"这条连接的这块终端"，用它兜底比按全局 caseID 猜更可靠。
	ActiveKey string
	// LastInputRejectAt 输入被丢弃的提示节流：用户连按回车时不必重复回包刷屏。
	LastInputRejectAt time.Time
}

// WSHandler 是单种 WS 消息的协议边缘处理器。原 switch case 内的 continue
// 语义等价于 return（每条消息独立派发，无 fallthrough）。
type WSHandler func(ctx *WSContext, msg models.InMessage)

var (
	wsRoutesMu sync.RWMutex
	wsRoutes   = map[string]WSHandler{}
)

// RegisterWSRoute 注册一种消息类型的处理器；重复注册直接 panic（启动期 fail-fast，
// 避免两个域静默争抢同一消息类型）。
func RegisterWSRoute(msgType string, h WSHandler) {
	wsRoutesMu.Lock()
	defer wsRoutesMu.Unlock()
	if _, dup := wsRoutes[msgType]; dup {
		panic("bridge: 重复注册 WS 消息路由: " + msgType)
	}
	wsRoutes[msgType] = h
}

// LookupWSRoute 查路由表；未知类型返回 nil（与旧 switch 无 default 分支的
// 静默忽略行为一致）。
func LookupWSRoute(msgType string) WSHandler {
	wsRoutesMu.RLock()
	defer wsRoutesMu.RUnlock()
	return wsRoutes[msgType]
}

// WSRoutesSnapshot 返回路由表快照（守卫测试冻结消息类型清单用）。
func WSRoutesSnapshot() map[string]WSHandler {
	wsRoutesMu.RLock()
	defer wsRoutesMu.RUnlock()
	out := make(map[string]WSHandler, len(wsRoutes))
	for k, v := range wsRoutes {
		out[k] = v
	}
	return out
}
