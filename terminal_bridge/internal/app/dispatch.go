package app

import (
	"sync"

	"terminal_bridge/internal/wsctx"
)

// 注册式 WS 消息路由（微内核）：app 只持有路由表与派发缝，不认识任何具体消息的
// 业务语义；各功能域在 init() 自注册 handler（R0c）。取代中央 switch 后，
// 「支持哪些消息类型」以路由表为唯一事实源，路由表守卫测试逐字冻结清单。

// LogCaseSubscriber 保留为别名：接口本体已归 shared/wsctx（域 handler 只依赖它，
// 不必为了拿一个两方法的接口而反向 import 装配壳）。
type LogCaseSubscriber = wsctx.Subscriber

// WSContext / WSHandler 保留为别名：A5 把这些类型下沉到 shared/wsctx，
// 装配层与既有测试的写法不用改，域侧从此可以只 import wsctx。
type WSContext = wsctx.Context
type WSHandler = wsctx.Handler

// Bridge 是 wsctx.Lifecycle 的唯一适配器：把这个断言放在装配层，
// 一旦生命周期方法签名漂移，编译期立刻失败，而不是等到某条 WS 消息在线上静默失效。
var _ wsctx.Lifecycle = (*Bridge)(nil)

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
