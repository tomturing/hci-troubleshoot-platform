package logcollect

// 传输证据端口的采集侧实现：交付域只表达"为这个 task_id 报进度/报包清单"，
// 票据、上传模式、TLS 放宽全部留在本域。
//
// 为什么不把 EvidenceConfig 直接交给交付域：交付域一次都没读过它的字段
// （只用它当不透明句柄透传），把整结构暴露出去等于把采集侧的安全策略
// 变成跨域契约，以后改票据或 TOS 上传都会波及交付。

import (
	"context"
	"time"

	"terminal_bridge/internal/shared/evidence"
)

// TransferMetric 保留为域内别名：上报契约本体已归 shared/evidence。
type TransferMetric = evidence.TransferMetric

// transferReporter 无状态，可安全被并发调用；每次按 task_id 现查凭据，
// 凭据被清理（任务结束）后自动退化为 Enabled=false。
type transferReporter struct{}

// NewTransferReporter 由装配层注入到 shared/evidence。
func NewTransferReporter() evidence.TransferReporter {
	return transferReporter{}
}

func (transferReporter) Enabled(taskID string) bool {
	_, ok := EvidenceFor(taskID)
	return ok
}

func (transferReporter) ReportArchive(ctx context.Context, taskID, path string) {
	cfg, ok := EvidenceFor(taskID)
	if !ok {
		return
	}
	ReportLocalArchive(ctx, cfg, path)
}

func (transferReporter) ReportTransferMetric(ctx context.Context, taskID string, metric evidence.TransferMetric) {
	cfg, ok := EvidenceFor(taskID)
	if !ok {
		return
	}
	// 5s 是历史预算：速率采样每 2s 一次，投递超过 5s 只说明平台侧已不可达，
	// 继续等会与下一次采样串成雪崩。
	postCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if _, err := cfg.PostEvidence(postCtx, cfg.TaskPath("metrics"), metric, ""); err != nil {
		evidence.Emit("WARN", "log_collect.metric_failed", "传输速率样本上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "seq": metric.Seq, "error": err.Error(),
		})
	}
}

func (transferReporter) MetricInterval() time.Duration { return EvidenceMetricInterval }

func (transferReporter) StallSeconds() int { return EvidenceStallSeconds }
