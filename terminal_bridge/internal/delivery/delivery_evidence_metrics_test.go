package delivery

import (
	"context"
	"sync"
	"terminal_bridge/internal/shared/evidence"
	"testing"
	"time"
)

// captureTransfer 是端口测试替身：只收集样本，不碰 HTTP。
//
// 阶段 2 之前这个用例直接往 logcollect.EvidenceConfigs 里塞凭据、起 httptest
// 收 POST——那测的是采集域的投递实现，不是交付域的采样算法。
// 现在分两层：这里验采样（EMA/归零/停摆），投递链路由 logcollect 侧用例验。
type captureTransfer struct {
	mu      sync.Mutex
	metrics []evidence.TransferMetric
	enabled bool
}

func (c *captureTransfer) Enabled(string) bool { return c.enabled }

func (c *captureTransfer) ReportArchive(context.Context, string, string) {}

func (c *captureTransfer) ReportTransferMetric(_ context.Context, _ string, metric evidence.TransferMetric) {
	c.mu.Lock()
	c.metrics = append(c.metrics, metric)
	c.mu.Unlock()
}

func (c *captureTransfer) MetricInterval() time.Duration { return 2 * time.Second }

func (c *captureTransfer) StallSeconds() int { return 60 }

func (c *captureTransfer) samples() []evidence.TransferMetric {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]evidence.TransferMetric, len(c.metrics))
	copy(out, c.metrics)
	return out
}

func TestStreamEvidenceMetricsEmitsSampleWithoutFakingProgress(t *testing.T) {
	capture := &captureTransfer{enabled: true}
	evidence.SetTransfer(capture)
	t.Cleanup(evidence.ResetTransfer)

	runner := &deliveryRunner{jobs: map[string]*deliveryJob{}}
	job := &deliveryJob{
		TaskID: "task-metrics-test", Phase: phasePulling, SourceSize: 1000,
		PulledBytes: 100, ChunkSize: 100,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4600*time.Millisecond)
	defer cancel()
	done := make(chan struct{})
	go func() {
		runner.streamEvidenceMetrics(ctx, job)
		close(done)
	}()
	<-done

	bodies := capture.samples()
	if len(bodies) == 0 {
		t.Fatal("2 秒节拍至少要上报一个样本")
	}
	first := bodies[0]
	if first.Stage != "fetching" || first.BytesDone != 100 || first.Source != "bridge" {
		t.Fatalf("样本事实错误: %+v", first)
	}
	if first.Percent == nil || *first.Percent != 10 {
		t.Fatalf("有分母时应给出百分比，实际 %+v", first.Percent)
	}
	if first.RateBps == nil || *first.RateBps <= 0 {
		t.Fatalf("首个样本有字节增量时应给出速率，实际 %+v", first.RateBps)
	}
	if len(bodies) < 2 {
		t.Fatalf("4 秒窗口应至少有两个样本，实际 %d", len(bodies))
	}
	if bodies[1].RateBps != nil {
		t.Fatalf("零增量时速率必须归零/省略，不得用 EMA 残留伪造速率: %+v", bodies[1].RateBps)
	}
	if bodies[1].EtaSec != nil {
		t.Fatalf("速率归零时不得继续倒数 ETA: %+v", bodies[1].EtaSec)
	}
}

// TestStreamEvidenceMetricsSkipsWhenDisabled 未开启证据回传时不得空转采样。
func TestStreamEvidenceMetricsSkipsWhenDisabled(t *testing.T) {
	capture := &captureTransfer{enabled: false}
	evidence.SetTransfer(capture)
	t.Cleanup(evidence.ResetTransfer)

	runner := &deliveryRunner{jobs: map[string]*deliveryJob{}}
	job := &deliveryJob{TaskID: "no-evidence", Phase: phasePulling, SourceSize: 10}

	done := make(chan struct{})
	go func() {
		runner.streamEvidenceMetrics(context.Background(), job)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("未开启证据回传时必须立刻返回，不能挂着采样")
	}
	if got := capture.samples(); len(got) != 0 {
		t.Fatalf("未开启时不应产生样本，实际 %d 条", len(got))
	}
}
