// metrics.go 进程级计数器（自 server.go 拆出，Prometheus 文本渲染仍在 server）。
package app

// Metrics 进程级计数器集合（原子操作）。
type Metrics struct {
	LogsCollectedTotal  uint64
	LogsCollectErrors   uint64
	LogsReplayedTotal   uint64
	LogsDroppedTotal    uint64
	ExecCommandsTotal   uint64
	ExecCommandErrors   uint64
	SshConnectionsTotal uint64
	SshConnectionErrors uint64
	VmConsoleOpsTotal   uint64
	VmConsoleOpErrors   uint64
	SftpDownloadsTotal  uint64
	SftpDownloadErrors  uint64
}

// PromMetrics 全局计数器实例。
var PromMetrics Metrics
