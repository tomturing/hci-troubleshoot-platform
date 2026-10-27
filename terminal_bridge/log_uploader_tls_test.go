package main

// 桥日志直传在自签平台上的行为护栏。
//
// 现场回归（2026-10-27 18:40Z）：endpoint 是 https://acli.sangfor.com.cn:4443/api/bridge-logs/direct，
// 每一批都是 `tls: failed to verify certificate: x509: certificate signed by unknown authority`，
// 退避涨到 16 秒、队列积压 28 条。结果是交付出问题时平台侧没有任何桥日志，
// 排查只能人工拷贝客户机终端。
//
// 断言的是"自动放宽一次并记住该 endpoint"，而不是"无条件跳过校验"：
// 第一次仍按严格模式尝试，只有确认是证书不受信才放宽。

import (
	"io"
	stdlog "log"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestLogUploadRelaxesTLSForSelfSignedPlatform(t *testing.T) {
	var hits int32
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		if got := r.Header.Get("X-Bridge-Upload-Token"); got != "token-123456" {
			t.Errorf("令牌必须透传，实际 %q", got)
		}
		w.WriteHeader(http.StatusNoContent)
	})

	server := httptest.NewUnstartedServer(handler)
	// 严格模式的第一次握手必然失败，服务端会打一行 TLS 错误；这里静音，避免污染测试输出。
	server.Config.ErrorLog = stdlog.New(io.Discard, "", 0)
	server.StartTLS()
	defer server.Close()

	endpoint := server.URL + "/api/bridge-logs/direct"
	uploader := newLogUploader()

	response, err := uploader.sendBatch([]byte("payload"), endpoint, "token-123456")
	if err != nil {
		t.Fatalf("自签平台应自动放宽证书校验并发送成功，实际: %v", err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("状态码应为 204，实际 %d", response.StatusCode)
	}
	if !uploader.isTLSRelaxed(endpoint) {
		t.Fatal("放宽决策必须被记住，否则每一批都会先失败一轮")
	}

	// 第二次直接走宽松模式：服务端只应看到 2 次成功请求（第一次放宽 + 第二次直接宽松），
	// 严格模式那次握手失败不会进 handler。
	second, err := uploader.sendBatch([]byte("payload"), endpoint, "token-123456")
	if err != nil {
		t.Fatalf("第二次发送应直接成功，实际: %v", err)
	}
	second.Body.Close()
	if got := atomic.LoadInt32(&hits); got != 2 {
		t.Fatalf("服务端应只收到 2 次成功请求，实际 %d", got)
	}
}

func TestLogUploadKeepsStrictTLSForUnknownFailures(t *testing.T) {
	// 非证书类失败不能被当成"自签平台"而放宽：只有确认为证书不受信才降级。
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	uploader := newLogUploader()
	response, err := uploader.sendBatch([]byte("payload"), server.URL+"/api/bridge-logs/direct", "token-123456")
	if err != nil {
		t.Fatalf("HTTP 500 属于可重试失败，应返回响应而不是连接错误: %v", err)
	}
	response.Body.Close()
	if uploader.isTLSRelaxed(server.URL + "/api/bridge-logs/direct") {
		t.Fatal("普通 5xx 不应触发 TLS 放宽")
	}
}
