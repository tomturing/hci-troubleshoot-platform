package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
	"golang.org/x/net/websocket"
)

func TestLineMatchesOutputFilterAllAndExclude(t *testing.T) {
	filter := OutputFilter{Source: "stdout", Include: []string{"4359974862144", "qcow2"}, Exclude: []string{"grep"}, IncludeMode: "all", CaseSensitive: true}
	if !lineMatchesOutputFilter("qemu 123 /images/4359974862144.vm/disk.qcow2\n", filter) {
		t.Fatal("expected the VM image line to match")
	}
	if lineMatchesOutputFilter("grep 4359974862144 qcow2\n", filter) {
		t.Fatal("exclude must win over include")
	}
}

func TestLineMatchesOutputFilterAnyCaseInsensitive(t *testing.T) {
	filter := OutputFilter{Source: "stdout", Include: []string{"server-img", "4359974862144"}, IncludeMode: "any", CaseSensitive: false}
	if !lineMatchesOutputFilter("SERVER-IMG is busy\n", filter) {
		t.Fatal("case-insensitive any mode should match")
	}
	if lineMatchesOutputFilter("unrelated process\n", filter) {
		t.Fatal("unrelated line must not match")
	}
}

func TestFiltersForSourceIgnoresEmptyAndOtherStream(t *testing.T) {
	filters := []OutputFilter{{Source: "stdout", Include: []string{"VM"}}, {Source: "stderr", Include: []string{"error"}}, {Source: "stdout"}}
	selected := filtersForSource(filters, "stdout")
	if len(selected) != 1 || selected[0].Include[0] != "VM" {
		t.Fatalf("unexpected stdout filters: %#v", selected)
	}
}

func TestValidateOutputFilters(t *testing.T) {
	valid := []OutputFilter{{Source: "stdout", Include: []string{"4359974862144"}, IncludeMode: "all", CaseSensitive: true}}
	if err := validateOutputFilters(valid); err != nil {
		t.Fatalf("valid literal filter rejected: %v", err)
	}
	invalid := []OutputFilter{{Source: "invalid", Include: []string{"VM"}, IncludeMode: "all"}}
	if err := validateOutputFilters(invalid); err == nil {
		t.Fatal("invalid source must be rejected")
	}
}

func TestNormalizeRuntimeConfigDefaults(t *testing.T) {
	tests := []struct {
		name        string
		mode        string
		wantAddress string
		wantOrigin  string
	}{
		{name: "桌面模式", mode: desktopMode, wantAddress: "127.0.0.1", wantOrigin: "*"},
		{name: "集群模式", mode: clusterMode, wantAddress: "0.0.0.0", wantOrigin: "same-origin"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			config, err := normalizeRuntimeConfig(test.mode, "", defaultWSPort, "")
			if err != nil {
				t.Fatalf("normalizeRuntimeConfig() error = %v", err)
			}
			if config.ListenAddress != test.wantAddress {
				t.Fatalf("ListenAddress = %q, want %q", config.ListenAddress, test.wantAddress)
			}
			if len(config.AllowedOrigins) != 1 || config.AllowedOrigins[0] != test.wantOrigin {
				t.Fatalf("AllowedOrigins = %#v, want %q", config.AllowedOrigins, test.wantOrigin)
			}
		})
	}
}

func TestClusterModeOriginPolicy(t *testing.T) {
	config, err := normalizeRuntimeConfig(clusterMode, "", defaultWSPort, "")
	if err != nil {
		t.Fatal(err)
	}
	if !config.originAllowed("http://hci.local", "hci.local") {
		t.Fatal("同源 Origin 应被允许")
	}
	if config.originAllowed("https://evil.example", "hci.local") {
		t.Fatal("非同源 Origin 不应被允许")
	}
}

func TestClusterModeRejectsCrossOriginWebSocketRequest(t *testing.T) {
	config, err := normalizeRuntimeConfig(clusterMode, "", defaultWSPort, "")
	if err != nil {
		t.Fatal(err)
	}
	handler := newHTTPHandler(newBridge(), config)
	request := httptest.NewRequest(http.MethodGet, "http://hci.local/terminal-bridge", nil)
	request.Host = "hci.local"
	request.Header.Set("Origin", "https://evil.example")
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)

	if response.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusForbidden)
	}
}

func TestAcceptNewHostKeyRejectsFingerprintChange(t *testing.T) {
	knownHostsPath := filepath.Join(t.TempDir(), "known_hosts")
	t.Setenv("HCI_BRIDGE_HOST_KEY_POLICY", "accept-new")
	t.Setenv("HCI_BRIDGE_KNOWN_HOSTS_FILE", knownHostsPath)

	newPublicKey := func() ssh.PublicKey {
		_, privateKey, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatalf("生成临时测试密钥失败: %v", err)
		}
		publicKey, err := ssh.NewPublicKey(privateKey.Public())
		if err != nil {
			t.Fatalf("转换临时测试公钥失败: %v", err)
		}
		return publicKey
	}

	const address = "127.0.0.1:2222"
	remote := &net.TCPAddr{IP: net.ParseIP("127.0.0.1"), Port: 2222}
	firstKey := newPublicKey()
	changedKey := newPublicKey()

	acceptNewCallback, err := buildHostKeyCallback(address)
	if err != nil {
		t.Fatalf("创建 accept-new 回调失败: %v", err)
	}
	if err := acceptNewCallback(address, remote, firstKey); err != nil {
		t.Fatalf("首次出现的主机指纹应被接纳: %v", err)
	}

	verificationCallback, err := buildHostKeyCallback(address)
	if err != nil {
		t.Fatalf("重新加载 known_hosts 失败: %v", err)
	}
	if err := verificationCallback(address, remote, firstKey); err != nil {
		t.Fatalf("已记录的相同主机指纹应继续通过: %v", err)
	}
	if err := verificationCallback(address, remote, changedKey); err == nil {
		t.Fatal("同一地址的主机指纹变化必须被拒绝")
	} else if keyErr, ok := err.(*knownhosts.KeyError); !ok || len(keyErr.Want) == 0 {
		t.Fatalf("指纹变化应返回包含期望指纹的 KeyError: %T %v", err, err)
	}
}

func TestContextFromMessageAcceptsW3CRandomFlag(t *testing.T) {
	previous := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer otel.SetTextMapPropagator(previous)

	const traceID = "caa7e3e825ba4a606df189740be1118c"
	const spanID = "cbef2f8fb7e2d3a8"
	ctx := contextFromMessage(InMessage{
		TraceID:     traceID,
		Traceparent: "00-" + traceID + "-" + spanID + "-03",
	})
	spanContext := trace.SpanContextFromContext(ctx)

	if !spanContext.IsValid() || !spanContext.IsRemote() {
		t.Fatalf("03 trace-flags 应提取为有效远程父上下文: %#v", spanContext)
	}
	if spanContext.TraceID().String() != traceID || spanContext.SpanID().String() != spanID {
		t.Fatalf("父上下文标识不一致: trace_id=%s span_id=%s", spanContext.TraceID(), spanContext.SpanID())
	}
	if !spanContext.IsSampled() {
		t.Fatal("归一化 03 trace-flags 时必须保留 Sampled 位")
	}
}

func TestNormalizeTraceparentOnlyDropsW3CRandomFlag(t *testing.T) {
	const prefix = "00-caa7e3e825ba4a606df189740be1118c-cbef2f8fb7e2d3a8-"
	tests := map[string]string{
		"00": prefix + "00",
		"01": prefix + "01",
		"02": prefix + "00",
		"03": prefix + "01",
		"04": prefix + "04",
	}
	for inputFlags, expected := range tests {
		t.Run(inputFlags, func(t *testing.T) {
			actual := normalizeTraceparentForLegacyGo(prefix + inputFlags)
			if actual != expected {
				t.Fatalf("normalizeTraceparentForLegacyGo() = %q, want %q", actual, expected)
			}
		})
	}
}

func TestHealthAndStatusEndpoints(t *testing.T) {
	config, err := normalizeRuntimeConfig(clusterMode, "", defaultWSPort, "")
	if err != nil {
		t.Fatal(err)
	}
	handler := newHTTPHandler(newBridge(), config)

	for _, path := range []string{
		"/health/live",
		"/health/ready",
		"/status",
		"/metrics",
		"/terminal-bridge/health/live",
		"/terminal-bridge/health/ready",
		"/terminal-bridge/status",
		"/terminal-bridge/metrics",
	} {
		t.Run(path, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, path, nil)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			if response.Code != http.StatusOK {
				t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
			}
		})
	}

	metricsRequest := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	metricsResponse := httptest.NewRecorder()
	handler.ServeHTTP(metricsResponse, metricsRequest)
	if !strings.Contains(metricsResponse.Body.String(), "bridge_process_up 1") {
		t.Fatalf("metrics 缺少 bridge_process_up: %s", metricsResponse.Body.String())
	}
}

func TestLogHubPublishReturnsObservableFields(t *testing.T) {
	hub := &LogHub{
		cap:        10,
		subs:       make(map[*websocket.Conn]*bridgeSubscriber),
		instanceID: "bridge-test-instance",
	}

	entry := hub.publish(logEntry{Level: "INFO", Service: "terminal_bridge", Event: "test"})

	if entry.Type != "bridge_log" {
		t.Fatalf("Type = %q, want bridge_log", entry.Type)
	}
	if entry.Seq != 1 {
		t.Fatalf("Seq = %d, want 1", entry.Seq)
	}
	if entry.Timestamp == "" {
		t.Fatal("Timestamp 不应为空")
	}
	if entry.EventID == "" || entry.BridgeInstanceID == "" {
		t.Fatal("幂等事件字段不应为空")
	}
}

func TestBoundedCaptureKeepsHashAndBoundsMemory(t *testing.T) {
	capture := newBoundedCapture(4)
	capture.write([]byte("abcdef"))
	if capture.String() != "abcd" || !capture.truncated || capture.total != 6 {
		t.Fatalf("有界捕获结果异常: value=%q truncated=%v total=%d", capture.String(), capture.truncated, capture.total)
	}
	if len(capture.SHA256()) != 64 {
		t.Fatalf("SHA256 长度异常: %q", capture.SHA256())
	}
}

func TestRedactSensitiveText(t *testing.T) {
	redacted := redactSensitiveText(`password=secret token: abc {"private_key":"sensitive"}`)
	if strings.Contains(redacted, "secret") || strings.Contains(redacted, "sensitive") {
		t.Fatalf("敏感信息未被脱敏: %s", redacted)
	}
}
