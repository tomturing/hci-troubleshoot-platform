package acli

// 阶段二 manifest 通道的证书策略单测。
//
// 要证明的三件事，缺一条这个修复就不成立：
//  1. 自签平台（默认严格校验必然失败的场景）能在**不放宽授权位**的情况下拿到清单；
//  2. 放宽只发生在「证书不受信」这一类失败上，401/5xx 不得触发第二次请求；
//  3. 放宽必须回报 relaxed=true，否则调用方没有留痕依据——"悄悄放宽"等于
//     把一次中间人风险变成不可观测事件。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// tlsTestCaseID 与 manifestJSON 里固化的 case_id 保持一致，否则会先撞
// manifest_case_mismatch，测不到证书路径。
const tlsTestCaseID = "Q2026111800001"

// newAcliPluginTestTLSServer 造一个**自签证书**的平台端（httptest 的证书链
// 不在客户机信任库里，正好等价于现场自签平台）。
func newAcliPluginTestTLSServer(t *testing.T, status int, hits *int32) *httptest.Server {
	t.Helper()
	body := manifestJSON(t, newTestPluginDesired())
	return httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(hits, 1)
		if r.Header.Get("X-Plugin-Ticket") != "ticket-1" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if status != http.StatusOK {
			w.WriteHeader(status)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
}

func TestFetchAcliPluginManifestRelaxesOnSelfSigned(t *testing.T) {
	var hits int32
	server := newAcliPluginTestTLSServer(t, http.StatusOK, &hits)
	defer server.Close()

	manifest, relaxed, err := fetchAcliPluginManifest(nil, context.Background(), server.URL, "ticket-1", tlsTestCaseID, false)
	if err != nil {
		t.Fatalf("自签平台应放宽校验后成功，实际失败: %v", err)
	}
	if !relaxed {
		t.Fatalf("放宽过必须回报 relaxed=true，否则调用方无法留痕")
	}
	if manifest == nil || len(manifest.Plugins) != 1 || manifest.Plugins[0].Name != "demo" {
		t.Fatalf("清单内容不符: %+v", manifest)
	}
	// 严格那次在 TLS 握手阶段就被服务端拒掉（不进入 handler），只有放宽那次
	// 能落到 handler：因此 handler 只应命中 1 次。命中 2 次说明放宽后又重试了一次。
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("放宽后应恰好有 1 次请求落到平台，实际 %d", got)
	}
}

func TestFetchAcliPluginManifestStrictPathNeedsNoRelax(t *testing.T) {
	// 回环 http 被 validatePluginPlatformURL 明确放行（无公网中间人风险），
	// 用它证明"信任链成立时不触发放宽"。
	var hits int32
	body := manifestJSON(t, newTestPluginDesired())
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		_, _ = w.Write(body)
	}))
	defer server.Close()

	manifest, relaxed, err := fetchAcliPluginManifest(nil, context.Background(), server.URL, "ticket-1", tlsTestCaseID, false)
	if err != nil {
		t.Fatalf("信任链成立时不应失败: %v", err)
	}
	if relaxed {
		t.Fatalf("未发生证书失败时不得标记 relaxed")
	}
	if manifest == nil || len(manifest.Plugins) != 1 {
		t.Fatalf("清单内容不符: %+v", manifest)
	}
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("不应有重试，实际请求 %d 次", got)
	}
}

func TestFetchAcliPluginManifestDoesNotRelaxOnServerError(t *testing.T) {
	// 5xx 与证书无关：放宽校验也解决不了，重试只会把失败延迟一个 RTT。
	// 这里直接走已授权放宽的路径，确保请求能真正到达 HTTP 层（否则测的是握手）。
	var hits int32
	server := newAcliPluginTestTLSServer(t, http.StatusInternalServerError, &hits)
	defer server.Close()

	_, relaxed, err := fetchAcliPluginManifest(nil, context.Background(), server.URL, "ticket-1", tlsTestCaseID, true)
	if err == nil {
		t.Fatalf("5xx 必须报错")
	}
	if relaxed {
		t.Fatalf("5xx 不得标记 relaxed")
	}
	if code := acliPluginErrorCode(err); code != "manifest_http_error" {
		t.Fatalf("原因码应为 manifest_http_error，实际 %s", code)
	}
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("5xx 不得重试，实际请求 %d 次", got)
	}
}

func TestFetchAcliPluginManifestDoesNotRelaxOnUnauthorized(t *testing.T) {
	// 票据问题更不能用放宽校验掩盖：那会把鉴权失败伪装成网络问题。
	var hits int32
	server := newAcliPluginTestTLSServer(t, http.StatusOK, &hits)
	defer server.Close()

	_, relaxed, err := fetchAcliPluginManifest(nil, context.Background(), server.URL, "wrong-ticket", tlsTestCaseID, true)
	if err == nil {
		t.Fatalf("401 必须报错")
	}
	if relaxed {
		t.Fatalf("401 不得标记 relaxed")
	}
	if code := acliPluginErrorCode(err); code != "plugin_ticket_rejected" {
		t.Fatalf("原因码应为 plugin_ticket_rejected，实际 %s", code)
	}
	// 第一次请求就已经是 401（http 状态，不是 TLS 握手失败），不得再发第二次。
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("401 不得重试，实际请求 %d 次", got)
	}
}

func TestFetchAcliPluginManifestReportsRetryFailureNotHandshake(t *testing.T) {
	// 放宽后仍失败时，要报**放宽后**的原因：只报第一次的 x509 会把
	// "平台 5xx" 这类真因藏起来，排查方向会被彻底带偏。
	var hits int32
	server := newAcliPluginTestTLSServer(t, http.StatusBadGateway, &hits)
	defer server.Close()

	_, relaxed, err := fetchAcliPluginManifest(nil, context.Background(), server.URL, "ticket-1", tlsTestCaseID, false)
	if err == nil {
		t.Fatalf("放宽后 502 仍必须报错")
	}
	if relaxed {
		t.Fatalf("最终失败时不得标记 relaxed")
	}
	if code := acliPluginErrorCode(err); code != "manifest_http_error" {
		t.Fatalf("原因码应为放宽后的 manifest_http_error，实际 %s", code)
	}
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("只应有放宽那一次请求到达平台，实际 %d", got)
	}
}

func TestFetchAcliPluginManifestInsecureAuthorizedSkipsStrictAttempt(t *testing.T) {
	// 前端已显式授权放宽时，第一次就用放宽 client：不应再走一遍注定失败的严格请求。
	var hits int32
	server := newAcliPluginTestTLSServer(t, http.StatusOK, &hits)
	defer server.Close()

	manifest, relaxed, err := fetchAcliPluginManifest(nil, context.Background(), server.URL, "ticket-1", tlsTestCaseID, true)
	if err != nil {
		t.Fatalf("已授权放宽时应直接成功: %v", err)
	}
	if relaxed {
		t.Fatalf("已授权放宽不属于本次自动放宽，relaxed 应为 false")
	}
	if manifest == nil || len(manifest.Plugins) != 1 {
		t.Fatalf("清单内容不符: %+v", manifest)
	}
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("已授权放宽时只应请求 1 次，实际 %d", got)
	}
}

func TestFetchAcliPluginManifestRejectsInsecurePlatformURL(t *testing.T) {
	// 放宽证书不等于放开协议：公网明文仍必须被拒，否则票据在链路上裸奔。
	_, _, err := fetchAcliPluginManifest(nil, context.Background(), "http://hci.example.com", "ticket-1", tlsTestCaseID, false)
	if err == nil {
		t.Fatalf("公网 http 平台地址必须拒绝")
	}
	if code := acliPluginErrorCode(err); code != "platform_url_insecure" {
		t.Fatalf("原因码应为 platform_url_insecure，实际 %s", code)
	}
	// 错误详情里必须出现被拒的地址，便于现场核对。
	if !strings.Contains(err.Error(), "hci.example.com") {
		t.Fatalf("错误信息应带上被拒地址: %v", err)
	}
}
