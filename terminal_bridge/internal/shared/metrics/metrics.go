// Package metrics 是进程级计数器：只依赖标准库，任何层都可以引用。
//
// 为什么从 app 下沉：计数器是横切的观测原语，被 sftp / logcollect 等业务域直接
// 使用。留在装配层会迫使业务域反向 import app（阶段 2 禁令「域→app 为 0」），
// 而这与「业务域不该认识装配层」直接冲突。
package metrics

// Counters 进程级计数器集合（原子操作）。
type Counters struct {
	LogsCollectedTotal  uint64
	LogsCollectErrors   uint64
	LogsReplayedTotal   uint64
	LogsDroppedTotal    uint64
	ExecCommandsTotal   uint64
	ExecCommandErrors   uint64
	SshConnectionsTotal uint64
	SshConnectionErrors uint64
	SshResizeTotal      uint64
	SshResizeErrors     uint64
	VmConsoleOpsTotal   uint64
	VmConsoleOpErrors   uint64
	SftpDownloadsTotal  uint64
	SftpDownloadErrors  uint64
}

// PromMetrics 全局计数器实例。保留原变量名，便于调用点最小改动。
var PromMetrics Counters
