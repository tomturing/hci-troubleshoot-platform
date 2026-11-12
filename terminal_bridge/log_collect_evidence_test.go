package main

import (
	"archive/zip"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestConfigureEvidenceRequiresTicketAndPlatformURL(t *testing.T) {
	taskID := "task-configure-test"
	t.Cleanup(func() { evidenceConfigs.Delete(taskID) })

	if _, ok := evidenceFor(taskID); ok {
		t.Fatalf("未配置时不应存在证据上下文")
	}
	configureEvidence(InMessage{
		ExecID: taskID, EvidenceTicket: "t.1", TransferPlatformURL: "not-a-url",
	}, "")
	if _, ok := evidenceFor(taskID); ok {
		t.Fatalf("平台地址非法时不得启用证据上报")
	}
	configureEvidence(InMessage{
		ExecID: taskID, CaseID: "Q2026111200624", EvidenceTicket: "t.1",
		TransferPlatformURL: "https://hci.example/", RemoteURL: "http://5888.example",
		TraceID: "tr-1", TransferTLSInsecure: true,
	}, "ui-1")
	cfg, ok := evidenceFor(taskID)
	if !ok {
		t.Fatalf("合法配置应启用证据上报")
	}
	if cfg.PlatformURL != "https://hci.example" {
		t.Fatalf("平台地址应去掉尾部斜杠，实际 %q", cfg.PlatformURL)
	}
	if cfg.UploadMode != "acli_direct" {
		t.Fatalf("带 remote_url 时应判 P1，实际 %q", cfg.UploadMode)
	}
	if !cfg.TLSInsecure || cfg.CustomUI != "ui-1" {
		t.Fatalf("TLS 授权与 custom_ui 必须传递，实际 %+v", cfg)
	}
}

func TestPostEvidenceCarriesTicketAndTrace(t *testing.T) {
	var seen sync.Map
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.Store("path", r.URL.Path)
		seen.Store("ticket", r.Header.Get("X-Evidence-Ticket"))
		seen.Store("trace", r.Header.Get("X-Trace-ID"))
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		seen.Store("body", body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	cfg := evidenceConfig{
		TaskID: "task-http", PlatformURL: server.URL, Ticket: "t.2", TraceID: "tr-2",
	}
	ack, err := cfg.postEvidence(context.Background(), cfg.taskPath("metrics"), transferMetric{
		Seq: 7, Stage: "uploading", BytesDone: 1024, Source: "bridge",
	}, "")
	if err != nil {
		t.Fatalf("上报失败: %v", err)
	}
	if ack["ok"] != true {
		t.Fatalf("响应解析失败: %v", ack)
	}
	if value, _ := seen.Load("ticket"); value != "t.2" {
		t.Fatalf("证据票据缺失: %v", value)
	}
	if value, _ := seen.Load("trace"); value != "tr-2" {
		t.Fatalf("调用链缺失: %v", value)
	}
	if value, _ := seen.Load("path"); value != "/api/direct/log-collect/tasks/task-http/metrics" {
		t.Fatalf("路径错误: %v", value)
	}
	body, _ := seen.Load("body")
	facts := body.(map[string]any)
	if facts["stage"] != "uploading" || facts["seq"] != float64(7) {
		t.Fatalf("事实上报内容错误: %v", facts)
	}
}

func TestInspectLocalArchiveReadsCentralDirectoryOnly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "archive.zip")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := zip.NewWriter(file)
	for _, name := range []string{"logs/a.log", "logs/b.log", "meta.json"} {
		entry, createErr := writer.Create(name)
		if createErr != nil {
			t.Fatal(createErr)
		}
		_, _ = entry.Write([]byte("0123456789"))
	}
	_ = writer.Close()
	_ = file.Close()

	manifest := inspectLocalArchive(path)
	if !manifest.Readable || manifest.EntryCount != 3 || manifest.EntryTotalBytes != 30 {
		t.Fatalf("中央目录读取错误: %+v", manifest)
	}
	if manifest.ArchiveSize <= 0 || manifest.HasEncrypted {
		t.Fatalf("包体积/加密标记错误: %+v", manifest)
	}
	if len(manifest.Entries) != 3 || manifest.Entries[0].Name != "a.log" {
		t.Fatalf("条目清单错误: %+v", manifest.Entries)
	}
}

func TestInspectLocalArchiveUnreadableIsNotClaimedAsValid(t *testing.T) {
	path := filepath.Join(t.TempDir(), "broken.zip")
	if err := os.WriteFile(path, []byte("not a zip"), 0o600); err != nil {
		t.Fatal(err)
	}
	manifest := inspectLocalArchive(path)
	if manifest.Readable || manifest.EntryCount != 0 {
		t.Fatalf("读不出来的包不能标成可读: %+v", manifest)
	}
	if manifest.Evidence["reason"] == "" {
		t.Fatalf("失败原因必须留证")
	}
}

func TestMeasureNetworkUnreachableReportsLossNotZero(t *testing.T) {
	sample := measureNetwork(context.Background(), "http://127.0.0.1:1", false)
	if sample.Reachable || sample.LossRate != 1 || sample.SampleCount != evidenceProbeSamples {
		t.Fatalf("不可达目标应报告 100%% 丢包而不是 0: %+v", sample)
	}
	if sample.RTTAvgMS != nil {
		t.Fatalf("没有成功样本时不应伪造 RTT")
	}
}

func TestBandwidthAdvice(t *testing.T) {
	if got := bandwidthAdvice(0, 0, false); got == "" {
		t.Fatal("不可达时必须给客户提示")
	}
	if got := bandwidthAdvice(1024, 10*1024*1024, true); got == "" {
		t.Fatal("上行过低时必须提示换时段或换路径")
	}
	if got := bandwidthAdvice(10*1024*1024, 10*1024*1024, true); got != "" {
		t.Fatalf("链路良好时不应制造误导性告警: %s", got)
	}
}

func TestStreamEvidenceMetricsEmitsSampleWithoutFakingProgress(t *testing.T) {
	var mu sync.Mutex
	var bodies []transferMetric
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var metric transferMetric
		_ = json.NewDecoder(r.Body).Decode(&metric)
		mu.Lock()
		bodies = append(bodies, metric)
		mu.Unlock()
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	taskID := "task-metrics-test"
	evidenceConfigs.Store(taskID, evidenceConfig{
		TaskID: taskID, PlatformURL: server.URL, Ticket: "t.3", TraceID: "tr-3",
	})
	t.Cleanup(func() { evidenceConfigs.Delete(taskID) })

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
