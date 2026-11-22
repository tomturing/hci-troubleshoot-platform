// Package wsctx 是一次 WebSocket 连接在派发期的**上下文与端口**。
//
// 为什么它必须独立存在：A5 之前 18 个 handler 全在 `cmd/bridge/ws_routes.go`，
// 每个都要 `ctx.B *app.Bridge`——于是"把 handler 还给业务域"这件事被一个类型卡死：
// 域不能 import app（装配壳），而不 import app 就拿不到 ctx。
//
// 事实核查（2026-11-21）：`app.Bridge` 只是一层内嵌 `*session.Hub` 的薄壳，
// handler 真正用到的能力只有三类------会话表、会话生命周期、连接级状态。
// 把这三种能力定义成本包的结构体与接口后，域 handler 只依赖本包，反向边消失。
//
// 分层：本包 import session / sshx / models / websocket，不 import 任何业务域与 app。
//
// 为什么**不**放在 internal/shared：门禁禁令 1 要求 shared 原语不认识上层（sshx 是上层
// 适配器），而本包必须持有 *session.Hub / *sshx.SSHSession 才能在 handler 侧直接可用；
// 若强行把它们抽成接口，域函数签名（terminal.HandleXxx(*sshx.SSHSession)）也得跟着改，
// 属于过度设计。本包的真实身份是**协议边缘契约**（cmd/bridge 与各域之间的一层），
// 故独立于 shared 与 app 之外，域与装配层都可以 import 它，域间依赖边不受影响。
package wsctx

import (
	"time"

	"golang.org/x/net/websocket"

	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
)

// Subscriber 是本连接绑定的日志订阅者（case 归属由它记录）。
type Subscriber interface {
	BindCase(caseID string)
	BoundCase() string
}

// Lifecycle 是 handler 需要的会话生命周期能力。
//
// 为什么收成接口而不是直接传 *app.Bridge：接口由消费者定义，
// 装配层把 Bridge 传进来即可（它已具备这三个方法），域侧永远不需要认识 Bridge。
type Lifecycle interface {
	// DestroySession 立即销毁会话（终态原因由调用方给出）。
	DestroySession(key, reason, byConn string) bool
	// DestroyOrDetachSession 长任务进行中只摘表不腰斩，任务结束后自愈关闭。
	DestroyOrDetachSession(key, reason, byConn string) bool
	// AutoConnectNode 在指定节点上按消息里的凭据自动建链。
	AutoConnectNode(ws *websocket.Conn, msg models.InMessage, owned *session.OwnedTracker, connID, role string) *sshx.SSHSession
}

// Context 承载单条 WebSocket 连接的派发上下文：连接级状态由 handle() 信封
// 创建与维护，handler 只经此缝读写，不再依赖闭包捕获的裸局部变量。
type Context struct {
	// Hub 是会话表（Get/Set/Remove/Resolve/ResolveInput/SetLastAuth 等方法提升）。
	Hub *session.Hub
	// Life 是会话生命周期端口（销毁、摘表保留、自动建链）。
	Life   Lifecycle
	WS     *websocket.Conn
	CUI    string
	ConnID string
	Role   string
	Sub    Subscriber
	Owned  *session.OwnedTracker
	// ActiveKey 记录本连接最近一次 ssh_connect 建立的会话键（含 @nodeIP）。
	// 键盘输入属于"这条连接的这块终端"，用它兜底比按全局 caseID 猜更可靠。
	ActiveKey string
	// LastInputRejectAt 输入被丢弃的提示节流：用户连按回车时不必重复回包刷屏。
	LastInputRejectAt time.Time
}

// Handler 是单种 WS 消息的协议边缘处理器。原 switch case 内的 continue
// 语义等价于 return（每条消息独立派发，无 fallthrough）。
type Handler func(ctx *Context, msg models.InMessage)

// Route 是一条消息类型的注册项。
//
// 域通过 `Routes()` 返回自己的全部路由，装配层只做汇总——
// 这样"哪个域负责哪些消息"写在域内，改一条消息族不用再翻 500 行的装配文件。
type Route struct {
	Type    string
	Handler Handler
}
