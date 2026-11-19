// metrics.go terminal 域指标注入缝：装配层（main）注入真实计数器。
// R0 结构性 seam：terminal 不反向依赖 app 层的 promMetrics。
package terminal

var (
	// MetricsExecErrors exec 通道错误计数（默认 no-op，装配层覆写）。
	MetricsExecErrors = func(n uint64) {}
	// MetricsVmConsoleOpErrors 虚拟机控制台操作错误计数（默认 no-op，装配层覆写）。
	MetricsVmConsoleOpErrors = func(n uint64) {}
)
