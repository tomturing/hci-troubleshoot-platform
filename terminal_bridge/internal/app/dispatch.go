package app

import (
	"terminal_bridge/internal/wsctx"
)

// 注册式 WS 消息路由（微内核）：app 只做装配，不认识任何具体消息的
// 业务语义；各功能域在 init() 自注册 handler（R0c）。取代中央 switch 后，
// 「支持哪些消息类型」以路由表为唯一事实源，路由表守卫测试逐字冻结清单。

// LogCaseSubscriber 保留为别名：接口本体已归 wsctx（域 handler 只依赖它，
// 不必为了拿一个两方法的接口而反向 import 装配壳）。
type LogCaseSubscriber = wsctx.Subscriber

// WSContext / WSHandler 保留为别名：A5 把这些类型下沉到 wsctx，
// 装配层与既有测试的写法不用改，域侧从此可以只 import wsctx。
type WSContext = wsctx.Context
type WSHandler = wsctx.Handler

// Bridge 是 wsctx.Lifecycle 的唯一适配器：把这个断言放在装配层，
// 一旦生命周期方法签名漂移，编译期立刻失败，而不是等到某条 WS 消息在线上静默失效。
var _ wsctx.Lifecycle = (*Bridge)(nil)

// 路由表本体已下沉 internal/wsctx：域要自注册就必须能 import 表，
// 而域 import app 会撞禁令 6。以下三个函数保留为装配层入口（cmd 与守卫测试在用）。

// RegisterWSRoute 注册一种消息类型的处理器。
func RegisterWSRoute(msgType string, h WSHandler) { wsctx.Register(msgType, h) }

// RegisterWSRoutes 汇总注册一个域声明的全部路由（域自注册的接线点）。
func RegisterWSRoutes(rs []wsctx.Route) { wsctx.RegisterRoutes(rs) }

// LookupWSRoute 查路由表；未知类型返回 nil。
func LookupWSRoute(msgType string) WSHandler { return wsctx.Lookup(msgType) }

// WSRoutesSnapshot 返回路由表快照（守卫测试冻结消息类型清单用）。
func WSRoutesSnapshot() map[string]WSHandler { return wsctx.Snapshot() }
