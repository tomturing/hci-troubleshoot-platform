package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"terminal_bridge/internal/logcollect"
	"testing"
	"time"
)

func TestStreamEvidenceMetricsEmitsSampleWithoutFakingProgress(t *testing.T) {
	var mu sync.Mutex
	var bodies []logcollect.TransferMetric
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var metric logcollect.TransferMetric
		_ = json.NewDecoder(r.Body).Decode(&metric)
		mu.Lock()
		bodies = append(bodies, metric)
		mu.Unlock()
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	taskID := "task-metrics-test"
	logcollect.EvidenceConfigs.Store(taskID, logcollect.EvidenceConfig{
		TaskID: taskID, PlatformURL: server.URL, Ticket: "t.3", TraceID: "tr-3",
	})
	t.Cleanup(func() { logcollect.EvidenceConfigs.Delete(taskID) })

	runner := &deliveryRunner{jobs: map[string]*deliveryJob{}}
	job := &deliveryJob{
		TaskID: taskID, Phase: phasePulling, SourceSize: 1000,
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

	mu.Lock()
	defer mu.Unlock()
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
