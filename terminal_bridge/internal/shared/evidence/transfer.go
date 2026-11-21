package evidence

// 传输证据端口：交付域需要向"发起本次采集的那个任务"回传速率采样与包清单，
// 但它不该认识证据票据、上传模式、TLS 放宽这些采集侧细节。
//
// 阶段 2 之前交付域直接调用 logcollect.EvidenceFor / ReportLocalArchive /
// PostEvidence，为了"报一次进度"把整个采集域拖成了依赖项。这里把能力收成
// 四个方法：采集域实现，装配层注入，交付域只表达意图。
//
// 与 Sink 的区别：Sink 是"桥自身日志"的横切通道；TransferReporter 是
// "业务对象（采集任务）的进度回传"，生命周期绑定 task_id，不能混为一谈。

import (
	"context"
	"sync/atomic"
	"time"
)

// TransferMetric 是一次传输速率采样（平台按 seq 去重、按 stalled 判停摆）。
//
// 放在这里而不是采集域：它是**上报契约**，交付域负责采样与填充，
// 采集域负责投递。契约两端都必须认识它，因此归共享层。
type TransferMetric struct {
	Seq        int64    `json:"seq"`
	Stage      string   `json:"stage"`
	BytesDone  int64    `json:"bytes_done"`
	BytesTotal *int64   `json:"bytes_total,omitempty"`
	Percent    *float64 `json:"percent,omitempty"`
	ElapsedMS  int64    `json:"elapsed_ms"`
	RateBps    *int64   `json:"rate_bps,omitempty"`
	EtaSec     *int64   `json:"eta_sec,omitempty"`
	Stalled    bool     `json:"stalled"`
	Source     string   `json:"source"`
}

// TransferReporter 是采集侧为交付域提供的证据回传能力。
type TransferReporter interface {
	// Enabled 表示该 task_id 有可用的证据凭据（未开启证据回传的任务直接跳过采样）。
	Enabled(taskID string) bool
	// ReportLocalArchive 读本地包中央目录并上报一次清单（同任务同路径只做一次）。
	ReportArchive(ctx context.Context, taskID, path string)
	// ReportTransferMetric 投递一次速率采样；投递失败由实现方自行记录告警。
	ReportTransferMetric(ctx context.Context, taskID string, metric TransferMetric)
	// MetricInterval 是采样节奏：由采集侧的进度粒度决定，交付侧不自作主张。
	MetricInterval() time.Duration
	// StallSeconds 是"多久没有新字节算停摆"的阈值。
	StallSeconds() int
}

// nopTransfer 是未注入时的兜底：所有上报静默丢弃，Enabled 恒 false，
// 让交付域在测试与命令行场景下无需构造采集域。
type nopTransfer struct{}

func (nopTransfer) Enabled(string) bool                                          { return false }
func (nopTransfer) ReportArchive(context.Context, string, string)                {}
func (nopTransfer) ReportTransferMetric(context.Context, string, TransferMetric) {}
func (nopTransfer) MetricInterval() time.Duration                                { return 2 * time.Second }
func (nopTransfer) StallSeconds() int                                            { return 60 }

// transferHolder 让 atomic.Value 永远只存同一个具体类型。
//
// 直接 Store 接口值会存下"装箱后的具体类型"，注入 *captureTransfer 之后再
// 换成 nopTransfer 就会 panic（sync/atomic: inconsistently typed value）。
var transfer atomic.Value

// SetTransfer 由装配层在启动时调用一次；传 nil 视为不注入（保持 Nop）。
func SetTransfer(r TransferReporter) {
	if r == nil {
		return
	}
	transfer.Store(&transferHolder{reporter: r})
}

// Transfer 返回当前注入的传输证据上报器（未注入时返回 Nop）。
func Transfer() TransferReporter {
	if v := transfer.Load(); v != nil {
		return v.(*transferHolder).reporter
	}
	return nopTransfer{}
}

// ResetTransfer 撤销注入，回到 Nop。
//
// 只给测试用：端口是进程级单例，测试之间必须能相互隔离，
// 否则一个用例注入的采集侧实现会污染后续用例（表现为"没配证据也上报了"）。
func ResetTransfer() {
	transfer.Store(&transferHolder{reporter: nopTransfer{}})
}

type transferHolder struct{ reporter TransferReporter }
