package wsctx

import "sync"

// 路由注册表：**消息类型 → 处理器**的唯一事实源。
//
// 为什么表必须在本包而不是 app：A5 要让各域在 init() 里自注册自己负责的消息，
// 而域不能 import app（禁令 6）。表若留在装配层，域要么放弃自注册、要么违规，
// 二选一都是死路。放到中立层后，域、cmd/bridge、测试三方都只认识本包。

var (
	routesMu sync.RWMutex
	routes   = map[string]Handler{}
)

// Register 注册一种消息类型的处理器；重复注册直接 panic。
//
// 启动期 fail-fast：两个域静默争抢同一消息类型，比启动失败危险得多——
// 后者一次就暴露，前者只在特定消息路径上表现为"我的 handler 没跑"。
func Register(msgType string, h Handler) {
	routesMu.Lock()
	defer routesMu.Unlock()
	if _, dup := routes[msgType]; dup {
		panic("bridge: 重复注册 WS 消息路由: " + msgType)
	}
	routes[msgType] = h
}

// RegisterRoutes 批量注册一个域声明的全部路由，供装配层汇总时调用。
//
// 先整体校验、再一次性写入：逐条注册时若第 N 条 panic，前 N-1 条已进表，
// 会留下"半个域在线"的表——这种半注册状态比启动失败更难查。
// nil handler 同样视为编程错误立即 panic：注册成功但派发时空指针更难查。
func RegisterRoutes(rs []Route) {
	if len(rs) == 0 {
		return
	}
	routesMu.Lock()
	defer routesMu.Unlock()
	seen := make(map[string]struct{}, len(rs))
	for _, r := range rs {
		if r.Handler == nil {
			panic("bridge: 路由 handler 为空: " + r.Type)
		}
		if _, dup := routes[r.Type]; dup {
			panic("bridge: 重复注册 WS 消息路由: " + r.Type)
		}
		if _, dup := seen[r.Type]; dup {
			panic("bridge: 同一批路由内重复: " + r.Type)
		}
		seen[r.Type] = struct{}{}
	}
	for _, r := range rs {
		routes[r.Type] = r.Handler
	}
}

// Lookup 查路由表；未知类型返回 nil（与旧 switch 无 default 分支的静默忽略行为一致）。
func Lookup(msgType string) Handler {
	routesMu.RLock()
	defer routesMu.RUnlock()
	return routes[msgType]
}

// Snapshot 返回路由表副本（守卫测试冻结消息类型清单用）。
// 必须拷贝：调用方若直接改返回值，会绕过 Register 的重复检查污染全局表。
func Snapshot() map[string]Handler {
	routesMu.RLock()
	defer routesMu.RUnlock()
	out := make(map[string]Handler, len(routes))
	for k, v := range routes {
		out[k] = v
	}
	return out
}
