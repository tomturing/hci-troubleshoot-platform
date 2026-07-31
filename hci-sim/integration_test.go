package main

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

// startServerWith 启动一个 hci-sim 实例（随机端口），返回地址与停止函数。
func startServerWith(t *testing.T, opts serverOptions) (string, func()) {
	t.Helper()
	if opts.HostKeyPath == "" {
		opts.HostKeyPath = filepath.Join(t.TempDir(), "hostkey")
	}
	if opts.Listen == "" {
		opts.Listen = "127.0.0.1:0"
	}
	if opts.Fixtures == "" {
		opts.Fixtures = "./fixtures"
	}
	ln, err := startSimServer(opts)
	if err != nil {
		t.Fatalf("startSimServer: %v", err)
	}
	return ln.Addr().String(), func() { _ = ln.Close() }
}

func startTestServer(t *testing.T) (string, func()) {
	return startServerWith(t, serverOptions{AcceptAny: true})
}

// runExec 用 ssh.Client 直连 hci-sim（与 terminal_bridge 相同的 exec 路径），
// 执行命令并返回 stdout/stderr/exitCode/timedOut。clientTimeout 用于验证 timeout 变体。
func runExec(t *testing.T, addr, cmd string, env map[string]string, password string, clientTimeout time.Duration) (stdout, stderr string, exitCode int, timedOut bool) {
	t.Helper()
	clientConfig := &ssh.ClientConfig{
		User:            "sim",
		Auth:            []ssh.AuthMethod{ssh.Password(password)},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         5 * time.Second,
	}
	client, err := ssh.Dial("tcp", addr, clientConfig)
	if err != nil {
		t.Fatalf("ssh.Dial: %v", err)
	}

	session, err := client.NewSession()
	if err != nil {
		t.Fatalf("NewSession: %v", err)
	}
	defer session.Close()

	for k, v := range env {
		if err := session.Setenv(k, v); err != nil {
			t.Fatalf("Setenv %s: %v", k, err)
		}
	}

	var so, se bytes.Buffer
	session.Stdout = &so
	session.Stderr = &se

	if err := session.Start(cmd); err != nil {
		t.Fatalf("session.Start: %v", err)
	}

	doneCh := make(chan int, 1)
	go func() {
		err := session.Wait()
		code := 0
		if ee, ok := err.(*ssh.ExitError); ok {
			code = ee.ExitStatus()
		} else if err != nil {
			code = -1
		}
		doneCh <- code
	}()

	timedOut = false
	select {
	case exitCode = <-doneCh:
		// Wait 已返回、done 协程结束，读取共享缓冲区安全
		stdout = so.String()
		stderr = se.String()
	case <-time.After(clientTimeout):
		timedOut = true
		// 超时路径：Wait 协程仍在阻塞，不读取共享 so/se，避免 data race
	}
	// 仅当 Wait 已返回（非超时）时才关闭连接，避免与 Wait 并发访问底层连接。
	if !timedOut {
		_ = client.Close()
	}
	return stdout, stderr, exitCode, timedOut
}

// 1) positive：默认 scenario 命中含错误的日志，exit 0。
func TestIntegration_Positive(t *testing.T) {
	addr, stop := startTestServer(t)
	defer stop()

	so, se, code, to := runExec(t, addr, `acli log get --keyword "too many file"`, nil, "sim", 5*time.Second)
	if to {
		t.Fatal("positive 不应超时")
	}
	if code != 0 {
		t.Fatalf("exit_code=%d, stderr=%q", code, se)
	}
	if !strings.Contains(so, "too many file descriptors") {
		t.Fatalf("stdout 未含预期错误行: %q", so)
	}
}

// 2) negative：指定 scenario-0002 命中正常日志（Agent matcher 不应命中关键字）。
func TestIntegration_NegativeScenario(t *testing.T) {
	addr, stop := startTestServer(t)
	defer stop()

	so, _, code, to := runExec(t, addr, `acli log get --keyword "too many file"`,
		map[string]string{"HCI_SIM_SCENARIO_ID": "scenario-0002"}, "sim", 5*time.Second)
	if to {
		t.Fatal("negative 不应超时")
	}
	if code != 0 {
		t.Fatalf("exit_code=%d", code)
	}
	if !strings.Contains(so, "heartbeat ok") {
		t.Fatalf("stdout 未含正常日志: got=%q", so)
	}
	if strings.Contains(so, "too many file descriptors") {
		t.Fatalf("negative 不应返回错误行，场景隔离失效: %q", so)
	}
}

// 3) fail closed：未知命令必须 exit 127 + fixture_not_found，绝不返回空 stdout+0。
func TestIntegration_FailClosed(t *testing.T) {
	addr, stop := startTestServer(t)
	defer stop()

	so, se, code, to := runExec(t, addr, `acli vm list`, nil, "sim", 5*time.Second)
	if to {
		t.Fatal("unknown 不应超时")
	}
	if code != 127 {
		t.Fatalf("期望 exit 127，实际 %d", code)
	}
	if !strings.Contains(se, "fixture_not_found") {
		t.Fatalf("stderr 应含 fixture_not_found: stderr=%q stdout=%q", se, so)
	}
}

// 4) timeout：scenario-0003 的 timeout fixture 应挂起，3s 内不返回 exit-status（对端只能靠超时断开）。
func TestIntegration_Timeout(t *testing.T) {
	addr, stop := startTestServer(t)
	defer stop()

	_, _, _, to := runExec(t, addr, `acli log get --keyword x`,
		map[string]string{"HCI_SIM_SCENARIO_ID": "scenario-0003"}, "sim", 3*time.Second)
	if !to {
		t.Fatal("timeout fixture 应在 3s 内不返回 exit-status（模拟命令挂起）")
	}
}

// 5) 密码兼容：terminal_bridge 会把密码追加 sangfornetwork 后缀，server 应放行。
func TestIntegration_BridgePasswordCompat(t *testing.T) {
	addr, stop := startServerWith(t, serverOptions{User: "sim", Password: "sim"}) // 非 accept-any
	defer stop()

	so, _, code, to := runExec(t, addr, `acli log get --keyword "too many file"`, nil, "simsangfornetwork", 5*time.Second)
	if to {
		t.Fatal("不应超时")
	}
	if code != 0 {
		t.Fatalf("exit_code=%d", code)
	}
	if !strings.Contains(so, "too many file descriptors") {
		t.Fatalf("密码兼容下未命中 positive: %q", so)
	}
}
