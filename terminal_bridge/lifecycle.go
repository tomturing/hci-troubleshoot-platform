package main

// 连接与会话的生命周期事件。
//
// 第一性原理：一次「SSH 会话不见了」的故障，必须能回答三个问题——
//  1. 谁销毁的（哪条浏览器连接 / 远端 shell 退出 / 保活判定 / 前端显式断开）；
//  2. 什么时候销毁的（相对建立存在多久）；
//  3. 当时还剩多少会话（是孤例还是全清）。
//
// 2026-10-27 现场（Q2026102701197 / 归档 Q2026102700862）的日志里，
// 这三问全部无法回答：只有一句 `active_sessions=0`，只能靠「报错比断连晚 266ms」反推。
// 因此把连接与会话的生命周期收敛到本文件，统一产出 conn.* / session.* 事件。

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

// processStartedAt 是进程启动时刻：退出事件用它算 uptime，
// 与 bridge.startup 的 timestamp 一起构成"这个进程活了多久"的唯一真源。
var processStartedAt time.Time

// redirectStderrToFile 把 stderr 重定向到日志目录下的独立文件。
//
// 为什么必须做：Go 的 panic / runtime fatal 只写 stderr，而 log.SetOutput 只接管
// 标准库 log 包，不接管它。客户现场双击 exe 后控制台窗口一闪而过，
// 崩溃栈就永久丢失（2026-10-27 现场正是无法区分"被杀"与"panic"）。
func redirectStderrToFile() {
	dir := strings.TrimSpace(logHub.logDir)
	if dir == "" {
		return
	}
	path := filepath.Join(dir, "bridge-stderr.log")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	os.Stderr = file
}

// connSeq 是进程内连接序号：单调递增，便于按时间排序与人工对账。
var connSeq uint64

// 连接角色：区分「端口探测」与「业务连接」。
//
// 前端每次 checkBridgeRunning / resolveBridgeUrl 都会开一条 WS 并立刻关闭，
// 没有角色字段时，它们与「终端真断开」在日志里完全同形
// （本次现场 19 次 connected / 17 次 disconnected 无法归类，就是缺这个字段）。
const (
	connRoleUnknown  = "unknown"
	connRoleTerminal = "terminal"
	connRolePanel    = "panel"
)

// 会话销毁原因：写入 session.destroyed.reason，供云端按原因聚合与告警。
const (
	sessionReasonWSClosed         = "ws_closed"         // 拥有它的浏览器连接关闭（连坐清理）
	sessionReasonReconnect        = "reconnect"         // 同键再次 ssh_connect 覆盖
	sessionReasonRemoteEOF        = "remote_eof"        // 远端 shell 退出
	sessionReasonClientDisconnect = "client_disconnect" // 前端显式 ssh_disconnect
	sessionReasonKeepAlive        = "keepalive_failed"  // 保活探测判定链路失效
)

// newConnID 生成进程内唯一的连接标识。
func newConnID() string {
	seq := atomic.AddUint64(&connSeq, 1)
	return fmt.Sprintf("c%d-%d", time.Now().UnixMilli()%1000000, seq)
}

// sessionKeyNode 从会话键里取回节点 IP（`caseID@nodeIP` → `nodeIP`）。
//
// 键不含 `@` 时返回空串：这类会话来自建单集成流程（未带 node_ip，键退化为 caseID），
// 它们的 node_ip 字段只能为空，不能猜测。
func sessionKeyNode(key string) string {
	if idx := strings.Index(key, "@"); idx >= 0 {
		return key[idx+1:]
	}
	return ""
}

// roleOfMessageType 把消息类型映射为连接角色。
//
// 只认「能确定归属」的两类：SSH 终端族与日志收集族；
// 其余（resume / ping / pong）返回空串，表示不改变角色。
func roleOfMessageType(msgType string) string {
	switch msgType {
	case "ssh_connect", "ssh_input", "ssh_inject_command",
		"ssh_exec_command", "ssh_exec_process", "ssh_disconnect", "vm_console_op":
		return connRoleTerminal
	case "log_collect", "acli_sync":
		return connRolePanel
	default:
		return ""
	}
}

// destroySession 是销毁 SSH 会话的唯一入口：close + 摘表 + 留痕，三件事必须原子完成。
//
// 为什么必须收敛：历史实现里 close/remove 散落在 5 处（连坐清理、同键覆盖、
// 远端 EOF、显式断开、自动连接失败回滚），每处各自 log.Printf，字段不一，
// 于是「谁销毁了会话」这个问题在日志里永远查不到。
//
// 返回值：true 表示本次确实销毁了一条会话；false 表示键已不存在（落空）。
func (b *Bridge) destroySession(key, reason, byConn string) bool {
	session := b.get(key)
	if session == nil {
		// 落空本身是重要信号：说明调用方以为自己在销毁，实际键早已不在
		// （例如远端 EOF 已先走一遍清理），据此可判断清理链路是否重复。
		blog("WARN", "session.destroy_miss", "会话销毁落空：键已不存在", "", sessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
			"key":             key,
			"reason":          reason,
			"by_conn":         byConn,
			"active_sessions": b.sessionCount(),
			"session_keys":    b.sessionKeyList(8),
		})
		return false
	}
	ownerConn, ageSeconds := session.ownerConn, int64(session.ageSeconds())
	session.close()
	b.remove(key)
	blog("WARN", "session.destroyed", "SSH 会话被销毁", "", sessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
		"key":             key,
		"reason":          reason,
		"by_conn":         byConn,
		"owner_conn":      ownerConn,
		"age_seconds":     ageSeconds,
		"active_sessions": b.sessionCount(),
		"session_keys":    b.sessionKeyList(8),
	})
	return true
}

// logSessionCreated 记录会话建立（含建立它的连接与角色）。
//
// 与会话销毁事件成对出现，构成「建立—销毁」闭环；
// 缺了它就只能看到销毁，无法判断是「从没建立过」还是「建立后被清掉」。
func logSessionCreated(session *SSHSession, key, connID, role, source string) {
	if session == nil {
		return
	}
	session.ownerConn = connID
	session.createdAt = time.Now()
	blog("INFO", "session.created", "SSH 会话已建立", "", sessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
		"key":        key,
		"conn_id":    connID,
		"role":       role,
		"source":     source, // ssh_connect | auto_connect
		"address":    session.address,
		"simulation": session.simulation,
	})
}

// ageSeconds 返回会话存活秒数；未记录建立时间时返回 0。
func (s *SSHSession) ageSeconds() float64 {
	if s == nil || s.createdAt.IsZero() {
		return 0
	}
	return time.Since(s.createdAt).Seconds()
}

// resourceSnapshot 采集进程级资源事实，用于启动/退出/定时采样。
//
// 为什么需要：本次现场的「内存 95% / CPU 83%」只能事后从任务管理器看到，
// 桥自身没有任何采样，因此无法证明「崩溃是资源导致的」还是「被外部结束的」。
func resourceSnapshot() map[string]any {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	// ring/seq 的读写都在 LogHub.mu 下，这里必须持锁快照，不能裸读。
	logHub.mu.Lock()
	ringLen, logSeq := len(logHub.ring), logHub.seq
	logHub.mu.Unlock()
	// 只报 Go 运行时能确定性拿到的量：Sys 是进程向 OS 申请的总内存（含堆外），
	// 足以判断"是不是在持续膨胀"，且不需要按平台引入 syscall 分支。
	snapshot := map[string]any{
		"goroutines":    runtime.NumGoroutine(),
		"heap_alloc_mb": int64(mem.HeapAlloc / (1024 * 1024)),
		"heap_sys_mb":   int64(mem.HeapSys / (1024 * 1024)),
		"sys_mb":        int64(mem.Sys / (1024 * 1024)),
		"num_gc":        mem.NumGC,
		"log_ring":      ringLen,
		"log_seq":       logSeq,
	}
	return snapshot
}

// startResourceSampler 周期性记录资源采样，供事后判断是否为资源枯竭。
//
// 采样间隔故意放长（默认 5 分钟）：在 Atom 级机器上，采样本身也必须廉价；
// 每 5 分钟一条、单条约 200 字节，一天约 58KB，对 64MiB 轮转毫无压力。
func startResourceSampler(interval time.Duration) {
	if interval <= 0 {
		return
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			extra := resourceSnapshot()
			extra["sessions"] = sessionCountSnapshot()
			blog("INFO", "resource.sample", "桥侧资源采样", "", "", "", "", extra)
		}
	}()
}

// sessionCountSnapshot 返回当前会话数；供采样与启停事件复用。
func sessionCountSnapshot() int {
	if bridgeSingleton == nil {
		return 0
	}
	return bridgeSingleton.sessionCount()
}

// bridgeSingleton 指向进程内唯一的 Bridge，供无接收者的辅助函数读取。
// 只有 main() 写入一次，读取侧仅用于采样，不改状态。
var bridgeSingleton *Bridge

// currentSysMB 返回 Go 运行时向 OS 申请的总内存（MB），用于启动/退出快照。
func currentSysMB() int64 {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	return int64(mem.Sys / (1024 * 1024))
}
