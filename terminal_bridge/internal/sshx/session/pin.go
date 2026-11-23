package session

import (
	"terminal_bridge/internal/shared/runtime"
	"time"
)

// DefaultSessionPinTTL 是业务钉住的默认保留窗口：任务结束后仍保留 2 小时。
//
// 为什么是"任务结束后 2 小时"而不是"一直保留"：日志收集常常几十分钟，用户中途
// 关页面/切工单是常态，5 分钟的 idle TTL 必然让回来的人撞上"SSH 会话未连接"。
// 但也不能永不回收——节点上的 SSH 会话数有上限，孤儿会话会挤占别人的名额。
const DefaultSessionPinTTL = 2 * time.Hour

// SessionPinTTL 返回业务钉住时长；<=0 表示关闭钉住。
//
// 放在 session 包而不是 sshx 包：发放与消费钉住的都是会话这一层，
// 且 delivery 域只允许依赖端口与 session，不得 import sshx 具体实现（禁令 7）。
func SessionPinTTL() time.Duration {
	seconds := runtime.EnvIntOrDefault("HCI_BRIDGE_SESSION_PIN_TTL_SECONDS", int(DefaultSessionPinTTL/time.Second))
	return time.Duration(seconds) * time.Second
}
