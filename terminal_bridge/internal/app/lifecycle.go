package app

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
	"strconv"
	"strings"
	"sync/atomic"
	"terminal_bridge/internal/shared/obslog"
	bruntime "terminal_bridge/internal/shared/runtime"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"time"
)

// ProcessStartedAt 是进程启动时刻：退出事件用它算 uptime，
// 与 bridge.startup 的 timestamp 一起构成"这个进程活了多久"的唯一真源。
var ProcessStartedAt time.Time

// RedirectStderrToFile 把 stderr 重定向到日志目录下的独立文件。
//
// 为什么必须做：Go 的 panic / runtime fatal 只写 stderr，而 log.SetOutput 只接管
// 标准库 log 包，不接管它。客户现场双击 exe 后控制台窗口一闪而过，
// 崩溃栈就永久丢失（2026-10-27 现场正是无法区分"被杀"与"panic"）。
func RedirectStderrToFile() {
	dir := strings.TrimSpace(obslog.DefaultHub.LogDir)
	if dir == "" {
		return
	}
	path := filepath.Join(dir, stderrLogName)
	// 必须在打开句柄之前轮转：Windows 不允许 rename 一个仍被占用的文件。
	rotateStderrIfNeeded(path)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	os.Stderr = file
}

const (
	stderrLogName = "bridge-stderr.log"
	stderrGenName = "bridge-stderr.gen"
	// stderrLogMaxBytes 是单文件上限：启动时超过就轮转一代（.1 覆盖）。
	// 60MB 足以容纳数万条 panic 栈，同时远低于客户机磁盘告警阈值。
	stderrLogMaxBytes = 60 << 20
)

// rotateStderrIfNeeded 在启动时把超限的 stderr 文件轮转为 .1，并写代数标记。
//
// 为什么只在启动时轮转：运行期间 os.Stderr 一直持有句柄，Windows 不允许 rename
// 被占用的文件；而"长期运行累积"这一主因恰好可由每次启动重置一代覆盖。
// 代数（gen）供 obslog 补传侧拼进 event_id：轮转后偏移从 0 重新计数，
// 不加代数会与轮转前的同一偏移 event_id 碰撞，被平台按幂等规则误丢。
func rotateStderrIfNeeded(path string) {
	info, err := os.Stat(path)
	if err != nil || info.Size() < stderrLogMaxBytes {
		return
	}
	if err := os.Rename(path, path+".1"); err != nil {
		return
	}
	gen := strconv.FormatInt(time.Now().UnixNano(), 10)
	_ = os.WriteFile(filepath.Join(filepath.Dir(path), stderrGenName), []byte(gen), 0o600)
}

// connSeq 是进程内连接序号：单调递增，便于按时间排序与人工对账。
var connSeq uint64

// 连接角色：区分「端口探测」与「业务连接」。
//
// 前端每次 checkBridgeRunning / resolveBridgeUrl 都会开一条 WS 并立刻关闭，
// 没有角色字段时，它们与「终端真断开」在日志里完全同形
// （本次现场 19 次 connected / 17 次 disconnected 无法归类，就是缺这个字段）。
const (
	ConnRoleUnknown  = "unknown"
	ConnRoleTerminal = "terminal"
	ConnRolePanel    = "panel"
)

// defaultSessionIdleTTL 是空闲租约的默认时长：WS 断开后给会话的宽限期。
//
// 必须长到覆盖"浏览器刷新→重连"与"短暂网络抖动"，又不能长到让孤儿会话久留占用远端 shell。
// 默认 5 分钟：常规刷新在数秒内完成，统计类日志采集常在 1~2 分钟内结束。
const defaultSessionIdleTTL = 5 * time.Minute

// NewConnID 生成进程内唯一的连接标识。
func NewConnID() string {
	seq := atomic.AddUint64(&connSeq, 1)
	return fmt.Sprintf("c%d-%d", time.Now().UnixMilli()%1000000, seq)
}

// sessionKeyNode 从会话键里取回节点 IP（`caseID@nodeIP` → `nodeIP`）。
//
// 实现已归 session.NodeFromKey；保留本包装是因为 lifecycle 事件留痕大量使用，
// 且 lifecycle_test 直接测这个纯函数。
func sessionKeyNode(key string) string { return session.NodeFromKey(key) }

// RoleOfMessageType 把消息类型映射为连接角色。
//
// 只认「能确定归属」的两类：SSH 终端族与日志收集族；
// 其余（resume / ping / pong）返回空串，表示不改变角色。
func RoleOfMessageType(msgType string) string {
	switch msgType {
	case "ssh_connect", "ssh_input", "ssh_inject_command",
		"ssh_exec_command", "ssh_exec_process", "ssh_disconnect", "vm_console_op":
		return ConnRoleTerminal
	case "log_collect", "acli_sync":
		return ConnRolePanel
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
func (b *Bridge) DestroySession(key, reason, byConn string) bool {
	session := b.Get(key)
	if session == nil {
		// 落空本身是重要信号：说明调用方以为自己在销毁，实际键早已不在
		// （例如远端 EOF 已先走一遍清理），据此可判断清理链路是否重复。
		obslog.Emit("WARN", "session.destroy_miss", "会话销毁落空：键已不存在", "", SessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
			"key":             key,
			"reason":          reason,
			"by_conn":         byConn,
			"active_sessions": b.Count(),
			"session_keys":    b.Keys(8),
		})
		return false
	}
	ownerConn, ageSeconds := session.OwnerConn, int64(sshx.AgeSeconds(session))
	session.Close()
	b.Remove(key)
	// 租约簿记必须随会话一起清理，否则 reaper 会反复命中已不存在的键。
	b.forgetLease(key)
	obslog.Emit("WARN", "session.destroyed", "SSH 会话被销毁", "", SessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
		"key":             key,
		"reason":          reason,
		"by_conn":         byConn,
		"owner_conn":      ownerConn,
		"age_seconds":     ageSeconds,
		"active_sessions": b.Count(),
		"session_keys":    b.Keys(8),
	})
	return true
}

// ReleaseSession 释放一条连接的持有权：归零后进入空闲租约，**不销毁会话**。
//
// 这是与历史行为的关键差别：过去 WS 一断就 close+remove，导致刷新页面/网络抖动
// 会把正在被日志面板使用的会话一起带走（2026-10-27 14:37:49 的故障链）。
// 租约状态由 shared/session.Hub 持有，这里只负责事件留痕。
func (b *Bridge) ReleaseSession(key, byConn string) {
	b.Hub.Release(key)
	obslog.Emit("INFO", "session.idle", "会话已无持有者，进入空闲租约", "", SessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
		"key": key, "by_conn": byConn, "idle_ttl_seconds": int(SessionIdleTTL().Seconds()),
	})
}

// forgetLease 清掉租约簿记（销毁时必须调用，否则 reaper 会反复命中空键）。
func (b *Bridge) forgetLease(key string) { b.Hub.ForgetLease(key) }

// ReapIdleSessions 回收空闲超时的会话，返回回收数量。
//
// 只在 refs==0 且空闲超过 TTL 时销毁：远端已 EOF 的会话会先被 onExit 清掉，
// 这里只处理"连接都走了但会话还活着"的那一类。长任务/业务忙碌的保护逻辑
// （保护期内不计空闲）在 Hub.ReapCandidates 内。
func (b *Bridge) ReapIdleSessions(ttl time.Duration) int {
	victims := b.Hub.ReapCandidates(ttl)
	for _, key := range victims {
		b.DestroySession(key, sshx.SessionReasonIdleTTL, "")
	}
	return len(victims)
}

// SessionIdleTTL 返回空闲租约时长；<=0 表示关闭租约回收（退化为"永不主动销毁"，
// 仅建议在调试时使用，因为那样只能靠远端 EOF 或显式断开回收）。
func SessionIdleTTL() time.Duration {
	seconds := bruntime.EnvIntOrDefault("HCI_BRIDGE_SESSION_IDLE_TTL_SECONDS", int(defaultSessionIdleTTL/time.Second))
	return time.Duration(seconds) * time.Second
}

// detachSession 把会话摘出路由表，但**保留底层 SSH 连接**。
//
// 与 destroySession 的差别只有"是否 close"：重连覆盖时若旧会话正在跑长任务，
// 关闭连接等于把作业腰斩；摘表后新会话可以正常入表，旧连接由
// endLongTask 归零时自愈关闭（orphaned 标记）。
func (b *Bridge) detachSession(key, reason, byConn string) bool {
	session := b.Get(key)
	if session == nil {
		obslog.Emit("WARN", "session.destroy_miss", "会话摘表落空：键已不存在", "", SessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
			"key":             key,
			"reason":          reason,
			"by_conn":         byConn,
			"mode":            "detach",
			"active_sessions": b.Count(),
			"session_keys":    b.Keys(8),
		})
		return false
	}
	ownerConn, ageSeconds := session.OwnerConn, int64(sshx.AgeSeconds(session))
	session.MarkOrphaned()
	b.Remove(key)
	b.forgetLease(key)
	obslog.Emit("WARN", "session.detached", "会话已摘表但保留连接（长任务进行中，禁止腰斩）", "", SessionKeyCase(key), sessionKeyNode(key), "", map[string]any{
		"key":             key,
		"reason":          reason,
		"by_conn":         byConn,
		"owner_conn":      ownerConn,
		"age_seconds":     ageSeconds,
		"long_tasks":      session.LongTasks.Load(),
		"active_sessions": b.Count(),
		"session_keys":    b.Keys(8),
	})
	return true
}

// destroyOrDetachSession 是重连覆盖场景的统一入口：有长任务则摘表保留，否则正常销毁。
//
// 收敛到一个入口是为了让"重连有没有腰斩作业"这个问题在日志里可回答：
// 两条路径分别记 session.detached 与 session.destroyed，按 reason=reconnect 一查即知。
func (b *Bridge) DestroyOrDetachSession(key, reason, byConn string) bool {
	if session := b.Get(key); session != nil && session.HasLongTask() {
		return b.detachSession(key, reason, byConn)
	}
	return b.DestroySession(key, reason, byConn)
}

// StartSessionReaper 周期性回收空闲会话。
//
// 扫描间隔取 TTL 的 1/4（最少 15 秒）：既不会让过期会话多留太久，
// 也不会在 Atom 级机器上频繁抢锁。
func StartSessionReaper(bridge *Bridge, ttl time.Duration) {
	if bridge == nil || ttl <= 0 {
		return
	}
	interval := ttl / 4
	if interval < 15*time.Second {
		interval = 15 * time.Second
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			if reaped := bridge.ReapIdleSessions(ttl); reaped > 0 {
				obslog.Emit("INFO", "session.reaped", "空闲租约到期，已回收会话", "", "", "", "", map[string]any{
					"reaped": reaped, "active_sessions": bridge.Count(),
				})
			}
		}
	}()
}

// LogSessionCreated 记录会话建立（含建立它的连接与角色）。
//
// 与会话销毁事件成对出现，构成「建立—销毁」闭环；
// 缺了它就只能看到销毁，无法判断是「从没建立过」还是「建立后被清掉」。
func LogSessionCreated(s *sshx.SSHSession, key, connID, role, source string) {
	session.LogCreated(s, key, connID, role, source)
}

// resourceSnapshot 采集进程级资源事实，用于启动/退出/定时采样。
//
// 为什么需要：本次现场的「内存 95% / CPU 83%」只能事后从任务管理器看到，
// 桥自身没有任何采样，因此无法证明「崩溃是资源导致的」还是「被外部结束的」。
func resourceSnapshot() map[string]any {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	// ring/seq 的读写都在 obslog.LogHub.Mu 下，这里必须持锁快照，不能裸读。
	obslog.DefaultHub.Mu.Lock()
	ringLen, logSeq := len(obslog.DefaultHub.Ring), obslog.DefaultHub.Seq
	obslog.DefaultHub.Mu.Unlock()
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

// StartResourceSampler 周期性记录资源采样，供事后判断是否为资源枯竭。
//
// 采样间隔故意放长（默认 5 分钟）：在 Atom 级机器上，采样本身也必须廉价；
// 每 5 分钟一条、单条约 200 字节，一天约 58KB，对 64MiB 轮转毫无压力。
func StartResourceSampler(interval time.Duration) {
	if interval <= 0 {
		return
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			extra := resourceSnapshot()
			extra["sessions"] = sessionCountSnapshot()
			obslog.Emit("INFO", "resource.sample", "桥侧资源采样", "", "", "", "", extra)
		}
	}()
}

// sessionCountSnapshot 返回当前会话数；供采样与启停事件复用。
func sessionCountSnapshot() int {
	if BridgeSingleton == nil {
		return 0
	}
	return BridgeSingleton.Count()
}

// BridgeSingleton 指向进程内唯一的 Bridge，供无接收者的辅助函数读取。
// 只有 main() 写入一次，读取侧仅用于采样，不改状态。
var BridgeSingleton *Bridge

// CurrentSysMB 返回 Go 运行时向 OS 申请的总内存（MB），用于启动/退出快照。
func CurrentSysMB() int64 {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	return int64(mem.Sys / (1024 * 1024))
}
