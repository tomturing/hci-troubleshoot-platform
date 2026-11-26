package terminal

import (
	"sync/atomic"
	"testing"

	"terminal_bridge/internal/shared/metrics"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"terminal_bridge/internal/wsctx"
)

// TestValidatePtySize 锁定 PTY 尺寸边界校验：零值、负值与超大值必须拒绝。
func TestValidatePtySize(t *testing.T) {
	cases := []struct {
		name    string
		cols    int
		rows    int
		wantErr bool
	}{
		{"正常窗口 80x24", 80, 24, false},
		{"超宽屏 240x80", 240, 80, false},
		{"最小边界 2x2", sshx.MinPtyCols, sshx.MinPtyRows, false},
		{"最大边界 1000x500", sshx.MaxPtyCols, sshx.MaxPtyRows, false},
		{"列数为 0", 0, 24, true},
		{"行数为负", 80, -1, true},
		{"列数超上限", sshx.MaxPtyCols + 1, 24, true},
		{"行数超上限", 80, sshx.MaxPtyRows + 1, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := sshx.ValidatePtySize(tc.cols, tc.rows)
			if (err != nil) != tc.wantErr {
				t.Fatalf("ValidatePtySize(%d, %d) err=%v, wantErr=%v", tc.cols, tc.rows, err, tc.wantErr)
			}
		})
	}
}

// TestHandleResizeValidationAndMissingSession 验证 ssh_resize 的防错与可观测指标计数。
func TestHandleResizeValidationAndMissingSession(t *testing.T) {
	hub := session.NewHub()
	ctx := &wsctx.Context{
		Hub: hub,
		CUI: "localhost",
	}

	beforeErrs := atomic.LoadUint64(&metrics.PromMetrics.SshResizeErrors)
	beforeSeq := func() uint64 {
		obslog.DefaultHub.Mu.Lock()
		defer obslog.DefaultHub.Mu.Unlock()
		return obslog.DefaultHub.Seq
	}()

	// 1. 非法尺寸必须被拦截并累计错误指标
	handleResize(ctx, models.InMessage{
		Type:    "ssh_resize",
		CaseID:  "Q202611270001",
		Cols:    0,
		Rows:    24,
		TraceID: "trace-resize-invalid",
	})
	if got := atomic.LoadUint64(&metrics.PromMetrics.SshResizeErrors); got != beforeErrs+1 {
		t.Fatalf("非法尺寸应递增 SshResizeErrors: want %d, got %d", beforeErrs+1, got)
	}

	// 2. 会话不存在时记录 session_missing 且不 panic
	handleResize(ctx, models.InMessage{
		Type:    "ssh_resize",
		CaseID:  "Q202611270001",
		Cols:    120,
		Rows:    36,
		TraceID: "trace-resize-missing",
	})
	if got := atomic.LoadUint64(&metrics.PromMetrics.SshResizeErrors); got != beforeErrs+2 {
		t.Fatalf("会话缺失应递增 SshResizeErrors: want %d, got %d", beforeErrs+2, got)
	}

	// 验证结构化日志留痕（包含 trace_id）
	obslog.DefaultHub.Mu.Lock()
	foundRejected := false
	foundMissing := false
	for i := len(obslog.DefaultHub.Ring) - 1; i >= 0; i-- {
		entry := obslog.DefaultHub.Ring[i]
		if entry.Seq <= beforeSeq {
			break
		}
		if entry.Event == "ssh_resize.rejected" && entry.TraceID == "trace-resize-invalid" {
			foundRejected = true
		}
		if entry.Event == "ssh_resize.session_missing" && entry.TraceID == "trace-resize-missing" {
			foundMissing = true
		}
	}
	obslog.DefaultHub.Mu.Unlock()

	if !foundRejected {
		t.Fatal("未记录带 trace_id 的 ssh_resize.rejected 结构化日志")
	}
	if !foundMissing {
		t.Fatal("未记录带 trace_id 的 ssh_resize.session_missing 结构化日志")
	}
}
