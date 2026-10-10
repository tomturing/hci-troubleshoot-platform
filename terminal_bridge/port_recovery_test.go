package main

import (
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

// occupyPort 在 127.0.0.1 上占用一个具体端口，返回端口号与持有监听器。
// 用于构造“端口被陌生进程占用”的场景；调用方结束时需 Close 返回的 listener。
func occupyPort(t *testing.T) (int, net.Listener) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("占用测试端口失败: %v", err)
	}
	_, portStr, err := net.SplitHostPort(listener.Addr().String())
	if err != nil {
		listener.Close()
		t.Fatalf("解析测试端口失败: %v", err)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		listener.Close()
		t.Fatalf("转换测试端口失败: %v", err)
	}
	return port, listener
}

// serveLiveBridgeOn 在指定端口上启动一个仅响应 /health/live 的假 Bridge，
// 用于模拟“同机已有存活 Bridge”。返回关闭函数。
func serveLiveBridgeOn(t *testing.T, port int) func() {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/health/live", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"status":"ok"}`)
	})
	listener, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(port))
	if err != nil {
		t.Fatalf("启动假 Bridge 失败: %v", err)
	}
	server := &http.Server{Handler: mux}
	go func() {
		_ = server.Serve(listener)
	}()
	return func() {
		_ = server.Close()
	}
}

func desktopConfig(port int) runtimeConfig {
	return runtimeConfig{Mode: desktopMode, ListenAddress: "127.0.0.1", Port: port}
}

func TestIsAddressInUse(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"unix", &net.OpError{Op: "listen", Err: errors.New("bind: address already in use")}, true},
		{"windows", errors.New("listen tcp: Only one usage of each socket address (protocol/network address/port) is normally permitted."), true},
		{"other", errors.New("permission denied"), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isAddressInUse(tc.err); got != tc.want {
				t.Fatalf("isAddressInUse(%v)=%v, 期望 %v", tc.err, got, tc.want)
			}
		})
	}
}

func TestListenWithRetryNonInUseReturnsFast(t *testing.T) {
	// 非“端口占用”类错误应立即返回，不重试（此处用非法地址触发）。
	_, err := listenWithRetry("tcp", "this-is-not-a-valid-address:99999", portBindRetryAttempts)
	if err == nil {
		t.Fatalf("非法地址应返回错误")
	}
	if isAddressInUse(err) {
		t.Fatalf("非法地址错误不应被判定为端口占用: %v", err)
	}
}

func TestListenWithRetryConflictFails(t *testing.T) {
	port, holder := occupyPort(t)
	defer holder.Close()
	address := "127.0.0.1:" + strconv.Itoa(port)
	_, err := listenWithRetry("tcp", address, 1)
	if err == nil {
		t.Fatalf("端口被占用时应返回错误")
	}
	if !isAddressInUse(err) {
		t.Fatalf("应识别为端口占用错误, 实际: %v", err)
	}
}

func TestIsLiveBridge(t *testing.T) {
	// 存活 Bridge：/health/live 返回 status:ok。
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"status":"ok"}`)
	}))
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "http://")
	if !isLiveBridge(addr) {
		t.Fatalf("应将返回 status:ok 的端点识别为存活 Bridge")
	}

	// 非 Bridge：返回非 ok 状态。
	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, `{"status":"not-a-bridge"}`)
	}))
	defer bad.Close()
	if isLiveBridge(strings.TrimPrefix(bad.URL, "http://")) {
		t.Fatalf("非 Bridge 响应不应被判为存活 Bridge")
	}

	// 端口不可达：连接被拒 → 安全视为非存活。
	closedListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("分配临时端口失败: %v", err)
	}
	closedAddr := closedListener.Addr().String()
	closedListener.Close()
	if isLiveBridge(closedAddr) {
		t.Fatalf("不可达地址不应被判为存活 Bridge")
	}
}

func TestAcquireListenerFreePrimary(t *testing.T) {
	// 用一个 free 端口作为主端口：bind 成功即用主端口。
	freePort, holder := occupyPort(t)
	// 立即释放，使端口空闲（loopback 上 TIME_WAIT 由重试兜底）。
	holder.Close()
	cfg := desktopConfig(freePort)
	listener, resolved, alreadyRunning, err := acquireListener(cfg, true)
	if err != nil {
		t.Fatalf("空闲主端口应成功监听: %v", err)
	}
	if listener == nil {
		t.Fatalf("应返回有效监听器")
	}
	defer listener.Close()
	if alreadyRunning {
		t.Fatalf("空闲端口不应判定为已运行")
	}
	if resolved.Port != freePort {
		t.Fatalf("应使用主端口 %d, 实际 %d", freePort, resolved.Port)
	}
}

func TestAcquireListenerAlreadyRunning(t *testing.T) {
	// 主端口被“存活 Bridge”占用 → 幂等退出（alreadyRunning=true），不回退。
	port, holder := occupyPort(t)
	holder.Close() // 释放该临时监听，让 serveLiveBridgeOn 复用同端口
	cleanup := serveLiveBridgeOn(t, port)
	defer cleanup()

	cfg := desktopConfig(port)
	listener, _, alreadyRunning, err := acquireListener(cfg, true)
	if err != nil {
		t.Fatalf("存活 Bridge 场景不应报错: %v", err)
	}
	if listener != nil {
		listener.Close()
	}
	if !alreadyRunning {
		t.Fatalf("应识别为同机已运行 Bridge 并幂等退出")
	}
}

func TestAcquireListenerCustomPortNoBackupFailsFast(t *testing.T) {
	// allowBackup=false（显式自定义端口/cluster）：主端口被陌生进程占用 → fail-fast，不回退。
	port, holder := occupyPort(t)
	defer holder.Close()
	cfg := desktopConfig(port)
	listener, resolved, alreadyRunning, err := acquireListener(cfg, false)
	if err == nil {
		if listener != nil {
			listener.Close()
		}
		t.Fatalf("自定义端口被占用应 fail-fast 返回错误")
	}
	if alreadyRunning {
		t.Fatalf("非 Bridge 占用不应判为已运行")
	}
	if resolved.Port != port {
		t.Fatalf("fail-fast 时配置端口不应改变, 期望 %d 实际 %d", port, resolved.Port)
	}
}

func TestAcquireListenerFallbackToBackup(t *testing.T) {
	// 主端口被“非 Bridge”占用 + allowBackup=true → 回退到 backupWSPort。
	primaryPort, holder := occupyPort(t)
	defer holder.Close()

	// 预占 backupWSPort 检测其是否可用；可用才继续，否则跳过（避免 CI 端口冲突误报）。
	probe, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(backupWSPort))
	if err != nil {
		t.Skipf("备用端口 %d 在本环境不可用，跳过回退用例: %v", backupWSPort, err)
	}
	probe.Close()

	cfg := desktopConfig(primaryPort)
	listener, resolved, alreadyRunning, err := acquireListener(cfg, true)
	if err != nil {
		t.Fatalf("应回退到备用端口成功: %v", err)
	}
	if listener == nil {
		t.Fatalf("应返回备用端口监听器")
	}
	defer listener.Close()
	if alreadyRunning {
		t.Fatalf("非 Bridge 占用不应判为已运行")
	}
	if resolved.Port != backupWSPort {
		t.Fatalf("应回退到备用端口 %d, 实际 %d", backupWSPort, resolved.Port)
	}
}

func TestAcquireListenerBothPortsOccupied(t *testing.T) {
	// 主端口被非 Bridge 占用，且备用端口也被占用 → fail-fast，错误信息含两个端口。
	primaryPort, primaryHolder := occupyPort(t)
	defer primaryHolder.Close()

	backupHolder, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(backupWSPort))
	if err != nil {
		t.Skipf("备用端口 %d 在本环境不可用，跳过主备皆占用例: %v", backupWSPort, err)
	}
	defer backupHolder.Close()

	cfg := desktopConfig(primaryPort)
	listener, _, alreadyRunning, err := acquireListener(cfg, true)
	if err == nil {
		if listener != nil {
			listener.Close()
		}
		t.Fatalf("主备皆占用应 fail-fast 返回错误")
	}
	if alreadyRunning {
		t.Fatalf("非 Bridge 占用不应判为已运行")
	}
	if !strings.Contains(err.Error(), strconv.Itoa(primaryPort)) ||
		!strings.Contains(err.Error(), strconv.Itoa(backupWSPort)) {
		t.Fatalf("错误信息应同时提及主端口 %d 与备用端口 %d, 实际: %v", primaryPort, backupWSPort, err)
	}
}

func TestFindPortHoldersDoesNotPanic(t *testing.T) {
	// findPortHolders 为 best-effort 诊断，任何环境下都不得 panic；结果可为空。
	port, holder := occupyPort(t)
	defer holder.Close()
	holders := findPortHolders(port)
	if holders == nil {
		t.Fatalf("应返回非 nil 切片（可为空）")
	}
}
