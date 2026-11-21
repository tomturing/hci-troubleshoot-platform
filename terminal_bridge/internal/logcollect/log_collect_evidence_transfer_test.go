package logcollect

// 传输证据端口的采集侧实现测试。
//
// 为什么放在采集域：投递链路（票据头、任务路径、TLS 放宽、失败告警）全在这里，
// 交付域只负责采样。阶段 2 之前这份覆盖混在 delivery 的测试里，靠直接 import
// logcollect 才能跑，正是那条需要消除的依赖边。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"terminal_bridge/internal/shared/evidence"
)

func TestTransferReporterPostsMetricToTaskPath(t *testing.T) {
	var mu sync.Mutex
	var gotPath, gotTicket string
	var gotMetric TransferMetric
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotPath = r.URL.Path
		gotTicket = r.Header.Get("X-Evidence-Ticket")
		_ = json.NewDecoder(r.Body).Decode(&gotMetric)
		mu.Unlock()
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	const taskID = "task-transfer-reporter"
	EvidenceConfigs.Store(taskID, EvidenceConfig{
		TaskID: taskID, PlatformURL: server.URL, Ticket: "t.3", TraceID: "tr-3",
	})
	t.Cleanup(func() { EvidenceConfigs.Delete(taskID) })

	reporter := NewTransferReporter()
	if !reporter.Enabled(taskID) {
		t.Fatal("已注册凭据的任务必须 Enabled")
	}

	done := int64(100)
	reporter.ReportTransferMetric(context.Background(), taskID, TransferMetric{
		Seq: 1, Stage: "fetching", BytesDone: 42, BytesTotal: &done, Source: "bridge",
	})

	mu.Lock()
	defer mu.Unlock()
	if gotPath != "/api/direct/log-collect/tasks/"+taskID+"/metrics" {
		t.Fatalf("投递路径错误: %s", gotPath)
	}
	if gotTicket != "t.3" {
		t.Fatalf("票据头缺失或被改写: %q", gotTicket)
	}
	if gotMetric.BytesDone != 42 || gotMetric.Stage != "fetching" || gotMetric.Seq != 1 {
		t.Fatalf("样本内容不符: %+v", gotMetric)
	}
}

// TestTransferReporterDisabledForUnknownTask 没有凭据时不得投递，也不得 panic。
func TestTransferReporterDisabledForUnknownTask(t *testing.T) {
	reporter := NewTransferReporter()
	if reporter.Enabled("no-such-task") {
		t.Fatal("未注册任务必须 Enabled=false")
	}
	// 直接调用也不能发请求（没人监听，发出去只会拖 15s 超时）
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	reporter.ReportTransferMetric(ctx, "no-such-task", TransferMetric{Seq: 1})
	reporter.ReportArchive(ctx, "no-such-task", "/sf/x.zip")
}

// TestTransferReporterExposesCadence 采样节奏与停摆阈值由采集侧定义。
func TestTransferReporterExposesCadence(t *testing.T) {
	reporter := NewTransferReporter()
	if reporter.MetricInterval() != EvidenceMetricInterval {
		t.Fatalf("采样节奏应为 %s，实际 %s", EvidenceMetricInterval, reporter.MetricInterval())
	}
	if reporter.StallSeconds() != EvidenceStallSeconds {
		t.Fatalf("停摆阈值应为 %d，实际 %d", EvidenceStallSeconds, reporter.StallSeconds())
	}
}

// TestEvidencePortNopByDefault 未注入时端口必须安全退化。
func TestEvidencePortNopByDefault(t *testing.T) {
	evidence.ResetTransfer()
	t.Cleanup(evidence.ResetTransfer)
	reporter := evidence.Transfer()
	if reporter.Enabled("any") {
		t.Fatal("未注入时 Enabled 必须为 false")
	}
	reporter.ReportTransferMetric(context.Background(), "any", evidence.TransferMetric{})
}
