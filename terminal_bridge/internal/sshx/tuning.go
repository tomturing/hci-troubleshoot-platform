package sshx

// tuning.go —— SSH 会话调优常量与业务活跃窗口（自 acli.go/lifecycle.go 随域迁入）。

import (
	"time"

	bruntime "terminal_bridge/internal/shared/runtime"
)

const (
	// sshDialTimeout 是 SSH 建连超时（等价于原 ClientConfig.Timeout；改为显式建连后需要自带）。
	sshDialTimeout = 12 * time.Second
	// tcpKeepAlivePeriod 是 TCP 层保活周期：让中间设备看到这条连接一直在活动。
	tcpKeepAlivePeriod = 30 * time.Second
	// sshKeepAliveProbeTimeout 是单次应用层探测的等待上限。
	// SSH mux 在半开连接上写可能永久阻塞，不能无限等，否则保活 goroutine 自己卡死。
	sshKeepAliveProbeTimeout = 10 * time.Second
	// sshKeepAliveIntervalDefault 是应用层探测间隔（秒）。
	// 可用 HCI_BRIDGE_SSH_KEEPALIVE_SECONDS 覆盖，<=0 表示关闭应用层探测。
	sshKeepAliveIntervalDefault = 30
	// shellHeartbeatInterval 是长任务期间向交互式 shell 注入保活输入的间隔。
	//
	// 必须显著小于 HCI 的 TMOUT=600：留出抖动余量，同时不能太频繁
	// （每次注入都是一次 stdin 写，密集到秒级会干扰用户正在输入的命令）。
	// 取 120s 表示"即使连续丢两次心跳，第 6 分钟仍会补上"。
	shellHeartbeatInterval = 120 * time.Second
	// shellHeartbeatQuietWindow 是"用户刚敲过键"的静默窗口：窗口内跳过本轮心跳。
	//
	// 为什么需要：心跳注入的是 NUL，bash 里等于 set-mark 无害，但用户若正在
	// vi/less/top 等前台程序里操作，多出来的字节会干扰界面。跳过 30s 后
	// 下一次 tick 在 120s 后，距上次输入最多 150s，仍远小于 TMOUT=600。
	shellHeartbeatQuietWindow = 30 * time.Second
)

// DefaultSessionBusyGrace 是"业务活跃"保护窗口。
//
// 必须远大于采集轮询间隔（5s），又要在用户真的离开后自动过期：取 15 分钟。
// 它是 lease 式的自我失效，不像 longTasks 计数那样"忘减一次就永久保护"，
// 因此用它覆盖"采集进行中但无法包裹"的场景是安全的（2026-10-28 现场的直接教训）。
const DefaultSessionBusyGrace = 15 * time.Minute

// SessionBusyGrace 返回业务活跃窗口；<=0 表示关闭该保护（退回只看长任务计数）。
func SessionBusyGrace() time.Duration {
	seconds := bruntime.EnvIntOrDefault("HCI_BRIDGE_SESSION_BUSY_GRACE_SECONDS", int(DefaultSessionBusyGrace/time.Second))
	return time.Duration(seconds) * time.Second
}
