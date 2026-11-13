package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// 自签 HTTPS 数据面：严格校验必然失败，一比一复刻客户内网 4443 平台。
type fakeEvidencePlatform struct {
	server     *httptest.Server
	posts      atomic.Int64
	withTicket atomic.Int64
	without    atomic.Int64
}

func newFakeEvidencePlatform(t *testing.T) *fakeEvidencePlatform {
	t.Helper()
	fake := &fakeEvidencePlatform{}
	fake.server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			return
		}
		fake.posts.Add(1)
		if r.Header.Get("X-Evidence-Ticket") == "ticket-001" {
			fake.withTicket.Add(1)
		} else {
			fake.without.Add(1)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(fake.server.Close)
	return fake
}

func evidenceConfigForTLS(url, taskID string) evidenceConfig {
	return evidenceConfig{
		TaskID: taskID, CaseID: "Q2026111300155", PlatformURL: url,
		Ticket: "ticket-001", TraceID: "trace-evidence-tls", NodeIP: "172.28.25.1",
	}
}

func TestEvidencePostAgainstSelfSignedPlatformAutoRelaxesOnce(t *testing.T) {
	fake := newFakeEvidencePlatform(t)
	cfg := evidenceConfigForTLS(fake.server.URL, "task-evidence-tls-strict")
	evidenceTLSRelaxed.Delete(cfg.TaskID)
	t.Cleanup(func() { evidenceTLSRelaxed.Delete(cfg.TaskID) })

	// 前提校验：严格客户端确实被自签证书挡住，否则下面的「自动放宽」没有证明力。
	strictReq, _ := http.NewRequest(http.MethodGet, fake.server.URL, nil)
	if _, err := httpClientFor(false, 3*time.Second).Do(strictReq); err == nil || !isUntrustedCertificateError(err) {
		t.Fatalf("测试前提不成立：自签服务端应触发证书不受信，实际 %v", err)
	}

	out, err := cfg.postEvidence(context.Background(), "/api/direct/log-collect/tasks/x/network-probe",
		map[string]any{"target": "platform_data"}, "")
	if err != nil {
		t.Fatalf("命中自签证书后应自动放宽并送达证据: %v", err)
	}
	if out["ok"] != true {
		t.Fatalf("响应体未解析: %#v", out)
	}
	if !evidenceTLSInsecure(cfg) {
		t.Fatal("放宽状态必须按 task_id 记住，否则后续每类证据都要先失败一轮")
	}
	if fake.posts.Load() != 1 {
		t.Fatalf("应只有放宽后的那一次 POST 到达平台，实际 %d", fake.posts.Load())
	}
	if fake.withTicket.Load() != 1 || fake.without.Load() != 0 {
		t.Fatalf("放宽 TLS 不得放松鉴权：带票据 %d 无票据 %d", fake.withTicket.Load(), fake.without.Load())
	}

	// 第二次直接走放宽客户端：不能再出现严格握手失败，也不能丢票据。
	if _, err := cfg.postEvidence(context.Background(), "/api/direct/log-collect/tasks/x/metrics",
		map[string]any{"seq": 2}, ""); err != nil {
		t.Fatalf("第二次证据上报应直接成功: %v", err)
	}
	if fake.posts.Load() != 2 || fake.withTicket.Load() != 2 {
		t.Fatalf("两次上报都必须带票据：posts=%d ticket=%d", fake.posts.Load(), fake.withTicket.Load())
	}
}

func TestEvidenceTLSRelaxationNeverLeaksAcrossTasks(t *testing.T) {
	fake := newFakeEvidencePlatform(t)
	first := evidenceConfigForTLS(fake.server.URL, "task-evidence-tls-a")
	second := evidenceConfigForTLS(fake.server.URL, "task-evidence-tls-b")
	evidenceTLSRelaxed.Delete(first.TaskID)
	evidenceTLSRelaxed.Delete(second.TaskID)
	t.Cleanup(func() {
		evidenceTLSRelaxed.Delete(first.TaskID)
		evidenceTLSRelaxed.Delete(second.TaskID)
	})

	if _, err := first.postEvidence(context.Background(), "/api/direct/log-collect/tasks/a/network-probe",
		map[string]any{"target": "platform_data"}, ""); err != nil {
		t.Fatalf("首个任务应自动放宽: %v", err)
	}
	if evidenceTLSInsecure(second) {
		t.Fatal("放宽状态不得跨任务污染，另一任务必须重新严格校验")
	}
}

func TestEvidencePostDoesNotRelaxOnApplicationError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)
	cfg := evidenceConfigForTLS(server.URL, "task-evidence-http-500")
	evidenceTLSRelaxed.Delete(cfg.TaskID)
	t.Cleanup(func() { evidenceTLSRelaxed.Delete(cfg.TaskID) })

	if _, err := cfg.postEvidence(context.Background(), "/api/direct/log-collect/tasks/x/metrics",
		map[string]any{"seq": 1}, ""); err == nil {
		t.Fatal("HTTP 500 必须如实报错")
	}
	if evidenceTLSInsecure(cfg) {
		t.Fatal("应用错误不是证书问题，不得放宽证书校验")
	}
}
