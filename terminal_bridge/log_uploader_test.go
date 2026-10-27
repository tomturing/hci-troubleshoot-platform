package main

// 桥日志直传的单测。
//
// 守护三条不变量：
//  1. 有令牌才发送，无令牌时是彻底的空操作（旧前端零影响）；
//  2. 成功才推进队列，失败整批保留（断点语义）；
//  3. 批次编码是 gzip + JSON，字段原样保留（后端按 event_id 幂等）。

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func sampleLogEntry() logEntry {
	return logEntry{
		Level: "INFO", Service: "terminal_bridge", Event: "session.created",
		Message: "SSH 会话已建立", CaseID: "Q1", NodeIP: "10.0.0.1",
		Extra: map[string]any{"key": "Q1@10.0.0.1"},
	}
}

func TestEncodeLogBatchIsGzipJSON(t *testing.T) {
	payload, err := encodeLogBatch([]logEntry{sampleLogEntry()})
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	reader, err := gzip.NewReader(bytes.NewReader(payload))
	if err != nil {
		t.Fatalf("不是合法 gzip: %v", err)
	}
	raw, err := io.ReadAll(reader)
	if err != nil {
		t.Fatalf("解压失败: %v", err)
	}
	var body struct {
		Logs []logEntry `json:"logs"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("不是合法 JSON: %v", err)
	}
	if len(body.Logs) != 1 || body.Logs[0].Event != "session.created" {
		t.Fatalf("字段未原样保留: %+v", body.Logs)
	}
}

// 无令牌时 enqueue 必须是空操作：旧前端没有直传能力，不能因此积压内存。
func TestUploaderIgnoresEventsWithoutToken(t *testing.T) {
	uploader := newLogUploader()
	uploader.enqueue(sampleLogEntry())
	if got := uploader.queueLen(); got != 0 {
		t.Fatalf("无令牌时不应入队，实际 %d 条", got)
	}
	if uploader.enabled() {
		t.Fatal("无令牌时不应处于启用状态")
	}
}

// 成功路径：整批发出、带令牌头、队列清空。
func TestUploaderFlushesBatchOnSuccess(t *testing.T) {
	var hits int32
	var gotToken string
	var gotEntries int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		gotToken = r.Header.Get("X-Bridge-Upload-Token")
		reader, err := gzip.NewReader(r.Body)
		if err != nil {
			t.Errorf("请求体不是 gzip: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		raw, _ := io.ReadAll(reader)
		var body struct {
			Logs []logEntry `json:"logs"`
		}
		_ = json.Unmarshal(raw, &body)
		gotEntries = len(body.Logs)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	uploader := newLogUploader()
	// 直接注入字段而不是 configure：configure 会启动后台 worker，
	// 与本用例手动 flushOnce 形成竞态，导致同一批被两个 goroutine 同时取走。
	uploader.token = "tok-abc"
	uploader.endpoint = server.URL
	uploader.enqueue(sampleLogEntry())
	uploader.enqueue(sampleLogEntry())

	sent, err := uploader.flushOnce()
	if err != nil {
		t.Fatalf("发送失败: %v", err)
	}
	if sent != 2 || gotEntries != 2 {
		t.Fatalf("期望发送 2 条，实际 sent=%d server=%d", sent, gotEntries)
	}
	if gotToken != "tok-abc" {
		t.Fatalf("令牌头错误: %q", gotToken)
	}
	if got := uploader.queueLen(); got != 0 {
		t.Fatalf("成功后队列应清空，实际 %d", got)
	}
	if atomic.LoadInt32(&hits) != 1 {
		t.Fatalf("应只发一次，实际 %d", hits)
	}
}

// 失败路径：队列必须整批保留，等下一轮重试（后端按 event_id 幂等抵消重复）。
func TestUploaderKeepsQueueOnFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	uploader := newLogUploader()
	uploader.token = "tok-abc"
	uploader.endpoint = server.URL
	uploader.enqueue(sampleLogEntry())

	if _, err := uploader.flushOnce(); err == nil {
		t.Fatal("503 应返回错误")
	}
	if got := uploader.queueLen(); got != 1 {
		t.Fatalf("失败后队列应保留 1 条，实际 %d", got)
	}
}

// 退避必须递增且有上限，避免弱网下打爆平台。
func TestUploaderBackoffIsBounded(t *testing.T) {
	uploader := newLogUploader()
	first := uploader.backoff()
	if first != time.Second {
		t.Fatalf("首次退避期望 1s，实际 %v", first)
	}
	var last time.Duration
	for i := 0; i < 10; i++ {
		last = uploader.backoff()
	}
	if last > 60*time.Second {
		t.Fatalf("退避超过上限: %v", last)
	}
}
