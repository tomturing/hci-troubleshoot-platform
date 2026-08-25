package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
	"golang.org/x/net/websocket"
)

func TestOwnedSessionTrackerDrainCaseIncludesNodeSessions(t *testing.T) {
	tracker := newOwnedSessionTracker()
	defaultSession := &SSHSession{}
	nodeSession := &SSHSession{}
	otherSession := &SSHSession{}
	tracker.set("CASE-1", defaultSession)
	tracker.set("CASE-1@node-a", nodeSession)
	tracker.set("CASE-10@node-b", otherSession)

	drained := tracker.drainCase("CASE-1")
	if len(drained) != 2 || drained["CASE-1"] != defaultSession || drained["CASE-1@node-a"] != nodeSession {
		t.Fatalf("drainCase 未返回工单全部会话: %#v", drained)
	}
	remaining := tracker.drain()
	if len(remaining) != 1 || remaining["CASE-10@node-b"] != otherSession {
		t.Fatalf("drainCase 错误清理了其他工单会话: %#v", remaining)
	}
}

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

func TestLineMatchesOutputFilterExcludeAll(t *testing.T) {
	filter := OutputFilter{
		Source: "stdout", Include: []string{"检测到IP", "冲突"}, IncludeMode: "all",
		Exclude: []string{"测试数据", "模拟冲突"}, ExcludeMode: "all", CaseSensitive: true,
	}
	if !lineMatchesOutputFilter("检测到IP 发生冲突 测试数据\n", filter) {
		t.Fatal("exclude=all 时只出现一个排除词不应排除")
	}
	if lineMatchesOutputFilter("检测到IP 发生冲突 测试数据 模拟冲突\n", filter) {
		t.Fatal("exclude=all 时全部排除词同时出现必须排除")
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
	invalidExcludeMode := []OutputFilter{{Source: "stdout", Include: []string{"VM"}, IncludeMode: "all", ExcludeMode: "none"}}
	if err := validateOutputFilters(invalidExcludeMode); err == nil {
		t.Fatal("invalid exclude_mode must be rejected")
	}
}

func TestDrainExecPipeFilteredTracksRawAndFilteredStats(t *testing.T) {
	input := "unrelated process\nqemu 9527 /images/18864231143.vm/disk.qcow2\n"
	filter := OutputFilter{
		Source: "stdout", Include: []string{"18864231143"}, IncludeMode: "all", CaseSensitive: true,
	}
	capture := newBoundedCapture(4096)
	stats := newStreamStats(true)
	emitted := strings.Builder{}
	wg := sync.WaitGroup{}
	wg.Add(1)

	go drainExecPipeFiltered(
		strings.NewReader(input), capture, stats, func(chunk string) { emitted.WriteString(chunk) }, &wg, "stdout", []OutputFilter{filter},
	)
	wg.Wait()
	stats.finish()

	if stats.rawBytes != int64(len(input)) {
		t.Fatalf("rawBytes = %d, want %d", stats.rawBytes, len(input))
	}
	if stats.scannedLines != 2 || stats.keptLines != 1 {
		t.Fatalf("lines scanned=%d kept=%d, want 2/1", stats.scannedLines, stats.keptLines)
	}
	if capture.total >= stats.rawBytes {
		t.Fatalf("filtered bytes = %d, raw bytes = %d; filtering did not reduce output", capture.total, stats.rawBytes)
	}
	if capture.String() != "qemu 9527 /images/18864231143.vm/disk.qcow2\n" {
		t.Fatalf("unexpected filtered capture: %q", capture.String())
	}
	if emitted.String() != capture.String() {
		t.Fatalf("emitted output differs from safe capture: %q", emitted.String())
	}
	if stats.rawSHA256() == capture.SHA256() {
		t.Fatal("raw and filtered SHA-256 should differ when a line is removed")
	}
}

func TestDrainExecPipeTracksUnfilteredStats(t *testing.T) {
	input := "line one\nline two"
	capture := newBoundedCapture(4096)
	stats := newStreamStats(false)
	wg := sync.WaitGroup{}
	wg.Add(1)

	go drainExecPipe(strings.NewReader(input), capture, stats, func(string) {}, &wg)
	wg.Wait()
	stats.finish()

	if stats.rawBytes != capture.total || stats.rawSHA256() != capture.SHA256() {
		t.Fatal("unfiltered raw and returned stream statistics must be identical")
	}
	if stats.scannedLines != 2 || stats.keptLines != 2 {
		t.Fatalf("lines scanned=%d kept=%d, want 2/2", stats.scannedLines, stats.keptLines)
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

func TestSimulationLeaseCredentialDetection(t *testing.T) {
	tests := []struct {
		name string
		msg  InMessage
		want bool
	}{
		{name: "complete htp2 lease context", msg: InMessage{AuthType: "lease", ExecutionMode: "sim-ssh", Password: "htp2.payload.signature"}, want: true},
		{name: "bare token is insufficient", msg: InMessage{AuthType: "password", Password: "htp2.payload.signature"}, want: false},
		{name: "lease auth alone is insufficient", msg: InMessage{AuthType: "lease", Password: "opaque"}, want: false},
		{name: "execution mode alone is insufficient", msg: InMessage{ExecutionMode: "sim-ssh", Password: "opaque"}, want: false},
		{name: "real HCI password", msg: InMessage{AuthType: "password", Password: "ordinary"}, want: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isSimulationLeaseCredential(tt.msg); got != tt.want {
				t.Fatalf("isSimulationLeaseCredential()=%v want=%v", got, tt.want)
			}
		})
	}
}

func TestBuildAuthMethodsAcceptsLease(t *testing.T) {
	methods, err := buildAuthMethods(InMessage{AuthType: "lease", Password: "htp2.payload.signature"})
	if err != nil {
		t.Fatal(err)
	}
	if len(methods) != 2 {
		t.Fatalf("lease 应复用 password + keyboard-interactive，实际方法数=%d", len(methods))
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

func TestExecResultAlwaysIncludesZeroExitCode(t *testing.T) {
	payload, err := json.Marshal(OutMessage{Type: "exec_result", CaseID: "Q1", ExitCode: 0})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(payload), `"exit_code":0`) {
		t.Fatalf("exec_result must include zero exit_code: %s", payload)
	}
}

// ── vm_console_op（虚拟机控制台截图固定操作通道）测试 ─────────────────────────

const testVMConsoleCaptureID = "0f8fad5b-d9cb-469f-a165-70867728950e"

func validVMConsoleMessage(operation string) InMessage {
	return InMessage{
		Type: "vm_console_op", CaseID: "Q2026081900001", NodeIP: "10.0.0.1", ExecID: "exec-1",
		CaptureID: testVMConsoleCaptureID, Operation: operation,
		HostNodeID: "SIM-HCI-NODE-01", VMID: "271230001", Timeout: 20,
	}
}

// TestParseVMConsoleRequestAcceptsValidOperations 合法消息必须通过校验并收敛 timeout。
func TestParseVMConsoleRequestAcceptsValidOperations(t *testing.T) {
	for _, operation := range []string{vmConsoleOpCaptureBaseline, vmConsoleOpWakeDownKey} {
		req, errType := parseVMConsoleRequest(validVMConsoleMessage(operation), "hci.local")
		if errType != "" {
			t.Fatalf("合法 %s 请求被拒绝: %s", operation, errType)
		}
		if req.Operation != operation || req.HostNodeID != "SIM-HCI-NODE-01" || req.VMID != "271230001" || req.CaptureID != testVMConsoleCaptureID {
			t.Fatalf("请求字段解析异常: %#v", req)
		}
		if req.TimeoutSeconds != 20 {
			t.Fatalf("timeout 应保留为 20，实际 %d", req.TimeoutSeconds)
		}
	}
	// timeout 越界收敛
	outOfRange := validVMConsoleMessage(vmConsoleOpCaptureBaseline)
	outOfRange.Timeout = 600
	if req, errType := parseVMConsoleRequest(outOfRange, ""); errType != "" || req.TimeoutSeconds != vmConsoleMaxTimeoutSecs {
		t.Fatalf("超范围 timeout 应收敛为 %d: errType=%s seconds=%d", vmConsoleMaxTimeoutSecs, errType, req.TimeoutSeconds)
	}
	outOfRange.Timeout = 0
	if req, errType := parseVMConsoleRequest(outOfRange, ""); errType != "" || req.TimeoutSeconds != vmConsoleDefaultTimeout {
		t.Fatalf("缺省 timeout 应为 %d: errType=%s seconds=%d", vmConsoleDefaultTimeout, errType, req.TimeoutSeconds)
	}
}

// TestParseVMConsoleRequestRejectsUnknownOperation 未知 operation 一律 operation_invalid。
func TestParseVMConsoleRequestRejectsUnknownOperation(t *testing.T) {
	for _, operation := range []string{"", "screendump", "sendkey", "capture_wake", "run_command"} {
		msg := validVMConsoleMessage(operation)
		if _, errType := parseVMConsoleRequest(msg, ""); errType != vmConsoleErrOperationInvalid {
			t.Fatalf("operation=%q 应返回 %s，实际 %q", operation, vmConsoleErrOperationInvalid, errType)
		}
	}
}

// TestParseVMConsoleRequestRejectsInvalidTargets 非法目标标识全部 target_invalid，
// 校验发生在任何命令构造之前（parseVMConsoleRequest 为纯函数，不触发执行）。
func TestParseVMConsoleRequestRejectsInvalidTargets(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*InMessage)
	}{
		{name: "host_node_id 含 shell 字符", mutate: func(m *InMessage) { m.HostNodeID = "node;rm -rf /" }},
		{name: "host_node_id 含空格", mutate: func(m *InMessage) { m.HostNodeID = "node id" }},
		{name: "host_node_id 以符号开头", mutate: func(m *InMessage) { m.HostNodeID = "-node" }},
		{name: "host_node_id 超长", mutate: func(m *InMessage) { m.HostNodeID = strings.Repeat("a", 129) }},
		{name: "host_node_id 为空", mutate: func(m *InMessage) { m.HostNodeID = "" }},
		{name: "vm_id 非数字", mutate: func(m *InMessage) { m.VMID = "vm123" }},
		{name: "vm_id 含路径穿越", mutate: func(m *InMessage) { m.VMID = "../etc" }},
		{name: "vm_id 超长", mutate: func(m *InMessage) { m.VMID = strings.Repeat("9", 21) }},
		{name: "vm_id 为空", mutate: func(m *InMessage) { m.VMID = "" }},
		{name: "capture_id 非 UUID", mutate: func(m *InMessage) { m.CaptureID = "not-a-uuid" }},
		{name: "capture_id 含 shell 字符", mutate: func(m *InMessage) { m.CaptureID = "$(reboot).ppm" }},
		{name: "capture_id 为空", mutate: func(m *InMessage) { m.CaptureID = "" }},
		{name: "case_id 为空", mutate: func(m *InMessage) { m.CaseID = "" }},
		{name: "exec_id 为空", mutate: func(m *InMessage) { m.ExecID = "" }},
		{name: "node_ip 为空", mutate: func(m *InMessage) { m.NodeIP = "" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			msg := validVMConsoleMessage(vmConsoleOpCaptureBaseline)
			tt.mutate(&msg)
			if _, errType := parseVMConsoleRequest(msg, ""); errType != vmConsoleErrTargetInvalid {
				t.Fatalf("应返回 %s，实际 %q", vmConsoleErrTargetInvalid, errType)
			}
		})
	}
}

// TestVMConsoleFixedArgvTokens 断言固定 argv 逐 token 精确匹配设计文档 §3.2 形态。
func TestVMConsoleFixedArgvTokens(t *testing.T) {
	screendump := vmConsoleScreendumpArgv("SIM-HCI-NODE-01", "271230001", testVMConsoleCaptureID)
	wantScreendump := []string{
		"vtpsh", "create", "/nodes/SIM-HCI-NODE-01/qemu/271230001/monitor",
		"--command", "screendump /sf/data/local/hci-diagnosis/" + testVMConsoleCaptureID + ".ppm",
	}
	if len(screendump) != len(wantScreendump) {
		t.Fatalf("screendump argv token 数不一致: %#v", screendump)
	}
	for i := range wantScreendump {
		if screendump[i] != wantScreendump[i] {
			t.Fatalf("screendump argv[%d] = %q, want %q", i, screendump[i], wantScreendump[i])
		}
	}

	wake := vmConsoleWakeArgv("SIM-HCI-NODE-01", "271230001")
	wantWake := []string{
		"vtpsh", "create", "/nodes/SIM-HCI-NODE-01/qemu/271230001/monitor",
		"--command", "sendkey down",
	}
	if len(wake) != len(wake) || len(wake) != len(wantWake) {
		t.Fatalf("wake argv token 数不一致: %#v", wake)
	}
	for i := range wantWake {
		if wake[i] != wantWake[i] {
			t.Fatalf("wake argv[%d] = %q, want %q", i, wake[i], wantWake[i])
		}
	}

	if got := vmConsoleProbeArgv(testVMConsoleCaptureID)[1:]; got[0] != "-f" || got[1] != vmConsoleCapturePath(testVMConsoleCaptureID) {
		t.Fatalf("探测 argv 异常: %#v", vmConsoleProbeArgv(testVMConsoleCaptureID))
	}
	readArgv := vmConsoleReadArgv(testVMConsoleCaptureID)
	if readArgv[0] != "base64" || readArgv[1] != "-w0" || readArgv[2] != vmConsoleCapturePath(testVMConsoleCaptureID) {
		t.Fatalf("读取 argv 异常: %#v", readArgv)
	}
	cleanupArgv := vmConsoleCleanupArgv(testVMConsoleCaptureID)
	if cleanupArgv[0] != "rm" || cleanupArgv[1] != "-f" || cleanupArgv[2] != vmConsoleCapturePath(testVMConsoleCaptureID) {
		t.Fatalf("清理 argv 异常: %#v", cleanupArgv)
	}
}

// TestVMConsoleCommandQuoting 命令拼接只产生固定形态；Monitor 指令整体被单引号包裹，
// 且经入口校验的输入不可能携带 shell 控制字符（非法输入已被 parse 层拒绝）。
func TestVMConsoleCommandQuoting(t *testing.T) {
	command := vmConsoleCommandFromArgv(vmConsoleScreendumpArgv("SIM-HCI-NODE-01", "271230001", testVMConsoleCaptureID))
	want := "vtpsh create /nodes/SIM-HCI-NODE-01/qemu/271230001/monitor " +
		"--command 'screendump /sf/data/local/hci-diagnosis/" + testVMConsoleCaptureID + ".ppm'"
	if command != want {
		t.Fatalf("拼接命令不一致:\n got %q\nwant %q", command, want)
	}
	// 安全字符不额外加引号
	if got := vmConsoleShellQuote("base64"); got != "base64" {
		t.Fatalf("安全 token 不应被引用: %q", got)
	}
	// 即使未来校验放宽，含单引号的输入也会被 POSIX 安全引用
	if got := vmConsoleShellQuote("a'b c"); got != `'a'\''b c'` {
		t.Fatalf("单引号引用异常: %q", got)
	}
}

// startFakeSSHServer 启动进程内最小 SSH 服务端，按 handler 决定每条 exec 命令的输出。
func startFakeSSHServer(t *testing.T, handler func(command string) (stdout string, exitCode int)) (host string, port int) {
	t.Helper()
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(privateKey)
	if err != nil {
		t.Fatal(err)
	}
	config := &ssh.ServerConfig{
		PasswordCallback: func(ssh.ConnMetadata, []byte) (*ssh.Permissions, error) { return nil, nil },
	}
	config.AddHostKey(signer)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })

	go func() {
		for {
			conn, acceptErr := listener.Accept()
			if acceptErr != nil {
				return
			}
			go func(raw net.Conn) {
				serverConn, channels, requests, sshErr := ssh.NewServerConn(raw, config)
				if sshErr != nil {
					_ = raw.Close()
					return
				}
				defer serverConn.Close()
				go ssh.DiscardRequests(requests)
				for newChannel := range channels {
					if newChannel.ChannelType() != "session" {
						_ = newChannel.Reject(ssh.UnknownChannelType, "仅支持 session")
						continue
					}
					channel, channelRequests, acceptChannelErr := newChannel.Accept()
					if acceptChannelErr != nil {
						continue
					}
					go func() {
						defer channel.Close()
						for request := range channelRequests {
							switch request.Type {
							case "env":
								_ = request.Reply(true, nil)
							case "exec":
								_ = request.Reply(true, nil)
								var payload struct{ Command string }
								_ = ssh.Unmarshal(request.Payload, &payload)
								stdout, exitCode := handler(payload.Command)
								if stdout != "" {
									_, _ = io.WriteString(channel, stdout)
								}
								_, _ = channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{uint32(exitCode)}))
								return
							default:
								_ = request.Reply(false, nil)
							}
						}
					}()
				}
			}(conn)
		}
	}()

	host, portString, err := net.SplitHostPort(listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	port, err = strconv.Atoi(portString)
	if err != nil {
		t.Fatal(err)
	}
	return host, port
}

// newFakeSSHSession 建立指向进程内假 SSH 服务端的 Bridge 会话。
func newFakeSSHSession(t *testing.T, host string, port int) *SSHSession {
	t.Helper()
	t.Setenv("HCI_BRIDGE_HOST_KEY_POLICY", "insecure")
	session, err := newSSHSession(InMessage{
		CaseID: "Q2026081900001", Host: host, Port: port,
		Username: "root", AuthType: "password", Password: "test-password",
	})
	if err != nil {
		t.Fatalf("连接假 SSH 服务端失败: %v", err)
	}
	t.Cleanup(session.close)
	return session
}

// runVMConsoleOpSync 同步执行固定操作并收集结果（超时兜底防止测试悬挂）。
func runVMConsoleOpSync(t *testing.T, session *SSHSession, req vmConsoleRequest) OutMessage {
	t.Helper()
	var result OutMessage
	done := make(chan struct{})
	go func() {
		session.runVMConsoleOp(func(message OutMessage) {
			result = message
			close(done)
		}, req)
	}()
	select {
	case <-done:
		return result
	case <-time.After(30 * time.Second):
		t.Fatal("vm_console 操作未在预期时间内返回")
		return OutMessage{}
	}
}

func parseVMConsoleRequestOrFail(t *testing.T, msg InMessage) vmConsoleRequest {
	t.Helper()
	req, errType := parseVMConsoleRequest(msg, "hci.local")
	if errType != "" {
		t.Fatalf("测试消息校验失败: %s", errType)
	}
	return req
}

// TestVMConsoleCaptureBaselineEndToEnd 覆盖完整基线截图链路：
// screendump → test -f 探测 → base64 读取 → 本地解码 + SHA-256 → HTTP 直传 → rm -f 清理。
func TestVMConsoleCaptureBaselineEndToEnd(t *testing.T) {
	ppm := []byte("P6\n2 1\n255\n" + "\xff\x00\x00\x00\xff\x00")
	wantSHA256 := fmt.Sprintf("%x", sha256.Sum256(ppm))

	var mu sync.Mutex
	var commands []string
	execHandler := func(command string) (string, int) {
		mu.Lock()
		commands = append(commands, command)
		mu.Unlock()
		switch {
		case strings.Contains(command, "screendump"):
			return "", 0
		case strings.HasPrefix(command, "test -f"):
			return "", 0
		case strings.HasPrefix(command, "base64 -w0"):
			return base64.StdEncoding.EncodeToString(ppm) + "\n", 0
		case strings.HasPrefix(command, "rm -f"):
			return "", 0
		}
		return "", 127
	}

	var uploadBody []byte
	var uploadHeader http.Header
	var uploadURL string
	artifactServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		uploadURL = r.URL.String()
		uploadHeader = r.Header.Clone()
		body, _ := io.ReadAll(r.Body)
		uploadBody = body
		w.WriteHeader(http.StatusOK)
	}))
	defer artifactServer.Close()
	t.Setenv("PLATFORM_ARTIFACT_URL", artifactServer.URL)
	t.Setenv("PLATFORM_INTERNAL_API_TOKEN", "internal-test-token")

	host, port := startFakeSSHServer(t, execHandler)
	session := newFakeSSHSession(t, host, port)
	req := parseVMConsoleRequestOrFail(t, validVMConsoleMessage(vmConsoleOpCaptureBaseline))
	req.NodeIP = host

	result := runVMConsoleOpSync(t, session, req)

	if result.Type != "vm_console_result" || result.Operation != vmConsoleOpCaptureBaseline {
		t.Fatalf("结果消息类型/操作异常: %#v", result)
	}
	if result.ErrorType != "" || result.ExitCode != 0 || result.TimedOut {
		t.Fatalf("基线截图应成功: error_type=%q exit_code=%d timed_out=%v", result.ErrorType, result.ExitCode, result.TimedOut)
	}
	if result.UploadStatus != vmConsoleUploadOK {
		t.Fatalf("upload_status = %q, want %q", result.UploadStatus, vmConsoleUploadOK)
	}
	if result.SHA256 != wantSHA256 || result.SizeBytes != int64(len(ppm)) {
		t.Fatalf("sha256/size 不一致: sha256=%q size=%d", result.SHA256, result.SizeBytes)
	}
	if result.CaptureID != testVMConsoleCaptureID {
		t.Fatalf("capture_id 未回显: %q", result.CaptureID)
	}

	// HTTP 直传契约：路径、查询参数、鉴权与完整性头、原始字节
	if !strings.Contains(uploadURL, "/internal/vm-console/artifacts/"+testVMConsoleCaptureID+"?") {
		t.Fatalf("上传路径异常: %s", uploadURL)
	}
	for _, part := range []string{"kind=ppm", "case_id=Q2026081900001", "mode=online"} {
		if !strings.Contains(uploadURL, part) {
			t.Fatalf("上传查询参数缺少 %s: %s", part, uploadURL)
		}
	}
	if uploadHeader.Get("Authorization") != "Bearer internal-test-token" {
		t.Fatalf("缺少内部鉴权头: %#v", uploadHeader)
	}
	if uploadHeader.Get("X-Capture-Sha256") != wantSHA256 {
		t.Fatalf("X-Capture-Sha256 不一致: %q", uploadHeader.Get("X-Capture-Sha256"))
	}
	if uploadHeader.Get("Content-Type") != "application/octet-stream" {
		t.Fatalf("Content-Type 异常: %q", uploadHeader.Get("Content-Type"))
	}
	if string(uploadBody) != string(ppm) {
		t.Fatal("上传的 PPM 字节与捕获不一致")
	}

	// 执行顺序与清理：screendump → test -f → base64 → rm -f（无条件执行）
	mu.Lock()
	defer mu.Unlock()
	if len(commands) != 4 {
		t.Fatalf("命令数 = %d, want 4: %#v", len(commands), commands)
	}
	if !strings.Contains(commands[0], "screendump") || !strings.HasPrefix(commands[1], "test -f") ||
		!strings.HasPrefix(commands[2], "base64 -w0") || !strings.HasPrefix(commands[3], "rm -f") {
		t.Fatalf("固定操作顺序异常: %#v", commands)
	}
}

// TestVMConsoleCaptureDisabledUploadFailsClosed 未配置 PLATFORM_ARTIFACT_URL 时
// fail-closed 返回 artifact_upload_disabled，绝不降级 base64 over WS。
func TestVMConsoleCaptureDisabledUploadFailsClosed(t *testing.T) {
	ppm := []byte("P6\n1 1\n255\n\x10\x10\x10")
	var mu sync.Mutex
	commands := 0
	handler := func(command string) (string, int) {
		mu.Lock()
		commands++
		mu.Unlock()
		if strings.HasPrefix(command, "base64 -w0") {
			return base64.StdEncoding.EncodeToString(ppm) + "\n", 0
		}
		return "", 0
	}
	t.Setenv("PLATFORM_ARTIFACT_URL", "")
	t.Setenv("PLATFORM_INTERNAL_API_TOKEN", "")

	host, port := startFakeSSHServer(t, handler)
	session := newFakeSSHSession(t, host, port)
	req := parseVMConsoleRequestOrFail(t, validVMConsoleMessage(vmConsoleOpCaptureBaseline))

	result := runVMConsoleOpSync(t, session, req)

	if result.UploadStatus != vmConsoleUploadDisabled || result.ErrorType != vmConsoleUploadDisabled {
		t.Fatalf("未配置制品服务应 fail-closed: upload_status=%q error_type=%q", result.UploadStatus, result.ErrorType)
	}
	// 元数据仍保留 sha256/size（截图本身已成功捕获）
	if result.SHA256 == "" || result.SizeBytes != int64(len(ppm)) {
		t.Fatalf("截图元数据缺失: sha256=%q size=%d", result.SHA256, result.SizeBytes)
	}
	// 结果消息绝不携带 base64 图片内容（WS 只回元数据）
	if payload, err := json.Marshal(result); err != nil || strings.Contains(string(payload), base64.StdEncoding.EncodeToString(ppm)) {
		t.Fatalf("WS 结果不得携带图片内容: %s", payload)
	}
}

// TestVMConsoleCaptureUploadFailedKeepsMetadata 制品服务异常时保留 sha256/size 元数据。
func TestVMConsoleCaptureUploadFailedKeepsMetadata(t *testing.T) {
	ppm := []byte("P6\n1 1\n255\n\x20\x20\x20")
	handler := func(command string) (string, int) {
		if strings.HasPrefix(command, "base64 -w0") {
			return base64.StdEncoding.EncodeToString(ppm) + "\n", 0
		}
		return "", 0
	}
	artifactServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer artifactServer.Close()
	t.Setenv("PLATFORM_ARTIFACT_URL", artifactServer.URL)
	t.Setenv("PLATFORM_INTERNAL_API_TOKEN", "token")

	host, port := startFakeSSHServer(t, handler)
	session := newFakeSSHSession(t, host, port)
	req := parseVMConsoleRequestOrFail(t, validVMConsoleMessage(vmConsoleOpCaptureBaseline))

	result := runVMConsoleOpSync(t, session, req)

	if result.UploadStatus != vmConsoleUploadFailed || result.ErrorType != vmConsoleErrUploadFailed {
		t.Fatalf("上传失败分类异常: upload_status=%q error_type=%q", result.UploadStatus, result.ErrorType)
	}
	if result.SHA256 == "" || result.SizeBytes != int64(len(ppm)) {
		t.Fatal("上传失败必须保留 sha256/size 元数据")
	}
}

// TestVMConsoleCaptureCommandFailureStillCleansUp screendump 失败时同样无条件清理临时文件。
func TestVMConsoleCaptureCommandFailureStillCleansUp(t *testing.T) {
	var mu sync.Mutex
	var commands []string
	handler := func(command string) (string, int) {
		mu.Lock()
		commands = append(commands, command)
		mu.Unlock()
		if strings.Contains(command, "screendump") {
			return "monitor unavailable", 1
		}
		return "", 0
	}
	t.Setenv("PLATFORM_ARTIFACT_URL", "")

	host, port := startFakeSSHServer(t, handler)
	session := newFakeSSHSession(t, host, port)
	req := parseVMConsoleRequestOrFail(t, validVMConsoleMessage(vmConsoleOpCaptureBaseline))

	result := runVMConsoleOpSync(t, session, req)

	if result.ErrorType != vmConsoleErrCaptureFailed || result.ExitCode != 1 {
		t.Fatalf("screendump 失败分类异常: error_type=%q exit_code=%d", result.ErrorType, result.ExitCode)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(commands) != 2 || !strings.HasPrefix(commands[1], "rm -f") {
		t.Fatalf("失败后未无条件清理: %#v", commands)
	}
}

// TestVMConsoleWakeEndToEnd 唤醒操作只执行固定 sendkey down，不做任何上传。
func TestVMConsoleWakeEndToEnd(t *testing.T) {
	var mu sync.Mutex
	var commands []string
	handler := func(command string) (string, int) {
		mu.Lock()
		commands = append(commands, command)
		mu.Unlock()
		return "", 0
	}
	uploadRequests := 0
	artifactServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		uploadRequests++
		w.WriteHeader(http.StatusOK)
	}))
	defer artifactServer.Close()
	t.Setenv("PLATFORM_ARTIFACT_URL", artifactServer.URL)
	t.Setenv("PLATFORM_INTERNAL_API_TOKEN", "token")

	host, port := startFakeSSHServer(t, handler)
	session := newFakeSSHSession(t, host, port)
	req := parseVMConsoleRequestOrFail(t, validVMConsoleMessage(vmConsoleOpWakeDownKey))

	result := runVMConsoleOpSync(t, session, req)

	if result.ErrorType != "" || result.ExitCode != 0 {
		t.Fatalf("唤醒应成功: error_type=%q exit_code=%d", result.ErrorType, result.ExitCode)
	}
	if result.UploadStatus != vmConsoleUploadNotApplicable {
		t.Fatalf("唤醒的 upload_status 应为 not_applicable: %q", result.UploadStatus)
	}
	if uploadRequests != 0 {
		t.Fatalf("唤醒不应触发上传，实际 %d 次", uploadRequests)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(commands) != 1 || !strings.Contains(commands[0], "--command 'sendkey down'") {
		t.Fatalf("唤醒只能执行固定 sendkey down: %#v", commands)
	}
	if strings.Contains(commands[0], "screendump") {
		t.Fatal("唤醒操作不得触发截图")
	}
}

// TestVMConsoleWakeFailureClassified sendkey 非零退出归类为 wake_failed。
func TestVMConsoleWakeFailureClassified(t *testing.T) {
	handler := func(command string) (string, int) { return "denied", 1 }
	t.Setenv("PLATFORM_ARTIFACT_URL", "")
	host, port := startFakeSSHServer(t, handler)
	session := newFakeSSHSession(t, host, port)
	req := parseVMConsoleRequestOrFail(t, validVMConsoleMessage(vmConsoleOpWakeDownKey))

	result := runVMConsoleOpSync(t, session, req)

	if result.ErrorType != vmConsoleErrWakeFailed || result.ExitCode != 1 {
		t.Fatalf("唤醒失败分类异常: error_type=%q exit_code=%d", result.ErrorType, result.ExitCode)
	}
	if result.UploadStatus != vmConsoleUploadNotApplicable {
		t.Fatalf("唤醒失败的 upload_status 仍应为 not_applicable: %q", result.UploadStatus)
	}
}

// TestUploadVMConsolePPMContract 单元级直传契约（httptest）：
// 路径、查询参数、鉴权头、完整性头、原始字节与 2xx 判定。
func TestUploadVMConsolePPMContract(t *testing.T) {
	data := []byte("P6\n1 1\n255\n\x01\x02\x03")
	sha256Hex := fmt.Sprintf("%x", sha256.Sum256(data))
	var gotURL, gotAuth, gotSha, gotContentType, gotMethod string
	var gotBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotURL = r.URL.String()
		gotMethod = r.Method
		gotAuth = r.Header.Get("Authorization")
		gotSha = r.Header.Get("X-Capture-Sha256")
		gotContentType = r.Header.Get("Content-Type")
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusCreated)
	}))
	defer server.Close()
	t.Setenv("PLATFORM_ARTIFACT_URL", server.URL)
	t.Setenv("PLATFORM_INTERNAL_API_TOKEN", "unit-token")

	status, err := uploadVMConsolePPM(context.Background(), testVMConsoleCaptureID, "CASE-1", data, sha256Hex)
	if err != nil || status != vmConsoleUploadOK {
		t.Fatalf("上传应成功: status=%q err=%v", status, err)
	}
	if gotMethod != http.MethodPost || gotURL != "/internal/vm-console/artifacts/"+testVMConsoleCaptureID+"?case_id=CASE-1&kind=ppm&mode=online" {
		t.Fatalf("上传请求形态异常: method=%s url=%s", gotMethod, gotURL)
	}
	if gotAuth != "Bearer unit-token" || gotSha != sha256Hex || gotContentType != "application/octet-stream" {
		t.Fatalf("上传头异常: auth=%q sha=%q content_type=%q", gotAuth, gotSha, gotContentType)
	}
	if string(gotBody) != string(data) {
		t.Fatal("上传字节不一致")
	}

	// 未配置 PLATFORM_ARTIFACT_URL → fail-closed，不降级
	t.Setenv("PLATFORM_ARTIFACT_URL", "")
	status, err = uploadVMConsolePPM(context.Background(), testVMConsoleCaptureID, "CASE-1", data, sha256Hex)
	if err != nil || status != vmConsoleUploadDisabled {
		t.Fatalf("未配置制品服务应返回 %s: status=%q err=%v", vmConsoleUploadDisabled, status, err)
	}

	// 制品服务非 2xx → upload_failed
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer failing.Close()
	t.Setenv("PLATFORM_ARTIFACT_URL", failing.URL)
	status, err = uploadVMConsolePPM(context.Background(), testVMConsoleCaptureID, "CASE-1", data, sha256Hex)
	if err == nil || status != vmConsoleUploadFailed {
		t.Fatalf("非 2xx 应返回 %s: status=%q err=%v", vmConsoleUploadFailed, status, err)
	}
}

// TestVMConsoleOpDispatchRejectsInvalidMessageOverWebSocket 经 WebSocket 派发入口：
// 非法 operation 直接回 vm_console_result 拒绝，不建立任何 SSH 执行。
func TestVMConsoleOpDispatchRejectsInvalidMessageOverWebSocket(t *testing.T) {
	config, err := normalizeRuntimeConfig(desktopMode, "", defaultWSPort, "")
	if err != nil {
		t.Fatal(err)
	}
	handler := newHTTPHandler(newBridge(), config)
	server := httptest.NewServer(handler)
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/"
	conn, err := websocket.Dial(wsURL, "", "http://hci.local")
	if err != nil {
		t.Fatalf("WebSocket 连接失败: %v", err)
	}
	defer conn.Close()

	// 同一 WS 还会收到 bridge_log 回采消息，只提取 vm_console_result。
	receiveVMConsoleResult := func() OutMessage {
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			var raw string
			if err := websocket.Message.Receive(conn, &raw); err != nil {
				t.Fatalf("接收消息失败: %v", err)
			}
			var response OutMessage
			if err := json.Unmarshal([]byte(raw), &response); err != nil {
				t.Fatal(err)
			}
			if response.Type == "vm_console_result" {
				return response
			}
		}
		t.Fatal("未在预期时间内收到 vm_console_result")
		return OutMessage{}
	}

	msg := validVMConsoleMessage("arbitrary_command")
	payload, _ := json.Marshal(msg)
	if err := websocket.Message.Send(conn, string(payload)); err != nil {
		t.Fatal(err)
	}
	response := receiveVMConsoleResult()
	if response.ErrorType != vmConsoleErrOperationInvalid {
		t.Fatalf("非法 operation 应被入口拒绝: %#v", response)
	}
	if response.ExitCode != -1 {
		t.Fatalf("拒绝消息 exit_code 应为 -1: %d", response.ExitCode)
	}

	// 非法 host_node_id 同样 target_invalid 拒绝
	msg = validVMConsoleMessage(vmConsoleOpCaptureBaseline)
	msg.HostNodeID = "node;reboot"
	payload, _ = json.Marshal(msg)
	if err := websocket.Message.Send(conn, string(payload)); err != nil {
		t.Fatal(err)
	}
	response = receiveVMConsoleResult()
	if response.ErrorType != vmConsoleErrTargetInvalid {
		t.Fatalf("非法 host_node_id 应被入口拒绝: %#v", response)
	}
}
