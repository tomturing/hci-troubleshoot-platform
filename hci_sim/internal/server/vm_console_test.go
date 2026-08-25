package server

import (
	"context"
	"encoding/base64"
	"strings"
	"testing"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
	"hci_sim/internal/metrics"

	"crypto/ed25519"
	"crypto/rand"
	"golang.org/x/crypto/ssh"
)

const (
	vmConsoleTestNode      = "SIM-HCI-NODE-01"
	vmConsoleTestVMID      = "271230001"
	vmConsoleTestCaptureID = "0f8fad5b-d9cb-469f-a165-70867728950e"
)

func vmConsoleTestClaims() lease.Claims {
	return lease.Claims{VirtualNodeID: vmConsoleTestNode, FixtureVariant: "positive-realistic"}
}

func vmConsoleScreendumpCommand() string {
	return "vtpsh create /nodes/" + vmConsoleTestNode + "/qemu/" + vmConsoleTestVMID + "/monitor" +
		" --command 'screendump /sf/data/local/hci-diagnosis/" + vmConsoleTestCaptureID + ".ppm'"
}

func vmConsoleCapturePath() string {
	return "/sf/data/local/hci-diagnosis/" + vmConsoleTestCaptureID + ".ppm"
}

// TestSimulateVMConsoleCommandMatchesFixedShapes 只匹配与 Bridge 常量表逐 token
// 一致的固定操作形态。
func TestSimulateVMConsoleCommandMatchesFixedShapes(t *testing.T) {
	claims := vmConsoleTestClaims()
	tests := []struct {
		name    string
		command string
		wantOut bool // 是否期望非空 stdout（base64 读取）
	}{
		{name: "screendump", command: vmConsoleScreendumpCommand()},
		{name: "sendkey down", command: "vtpsh create /nodes/" + vmConsoleTestNode + "/qemu/" + vmConsoleTestVMID + "/monitor --command 'sendkey down'"},
		{name: "test -f 探测", command: "test -f " + vmConsoleCapturePath()},
		{name: "base64 读取", command: "base64 -w0 " + vmConsoleCapturePath(), wantOut: true},
		{name: "rm -f 清理", command: "rm -f " + vmConsoleCapturePath()},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stdout, exitCode, matched := simulateVMConsoleCommand(tt.command, claims)
			if !matched || exitCode != exitOK {
				t.Fatalf("固定操作未被仿真: matched=%v exit=%d", matched, exitCode)
			}
			if tt.wantOut && stdout == "" {
				t.Fatal("base64 读取应返回 PPM 内容")
			}
			if !tt.wantOut && stdout != "" {
				t.Fatalf("该操作不应有 stdout: %q", stdout)
			}
		})
	}
}

// TestSimulateVMConsoleCommandFailsClosedOnDeviations 任何偏离固定形态的命令都
// 不匹配（调用方落回严格 Fixture 路由，最终 fixture_not_found fail-closed）。
func TestSimulateVMConsoleCommandFailsClosedOnDeviations(t *testing.T) {
	claims := vmConsoleTestClaims()
	deviations := []string{
		// 其他 Monitor 指令
		"vtpsh create /nodes/" + vmConsoleTestNode + "/qemu/" + vmConsoleTestVMID + "/monitor --command 'sendkey up'",
		// 指向其他宿主机（租约归属校验）
		"vtpsh create /nodes/OTHER-NODE/qemu/" + vmConsoleTestVMID + "/monitor --command 'sendkey down'",
		// 额外参数
		vmConsoleScreendumpCommand() + " --extra",
		// 目录偏移
		"test -f /sf/data/local/hci-diagnosis/../../etc/passwd",
		"base64 -w0 /tmp/" + vmConsoleTestCaptureID + ".ppm",
		// 非 UUID 文件名
		"rm -f /sf/data/local/hci-diagnosis/arbitrary.ppm",
		// 读取参数变形
		"base64 -w 76 " + vmConsoleCapturePath(),
		// 完全无关的命令
		"acli --formatter=json task list",
	}
	for _, command := range deviations {
		if _, _, matched := simulateVMConsoleCommand(command, claims); matched {
			t.Fatalf("变形命令不应被仿真匹配: %q", command)
		}
	}
	// shell 操作符被受限 lexer 拒绝 → 同样不匹配
	if _, _, matched := simulateVMConsoleCommand("base64 -w0 "+vmConsoleCapturePath()+"; rm -rf /", claims); matched {
		t.Fatal("含 shell 操作符的命令不应被仿真匹配")
	}
}

// TestSimulateVMConsolePPMVariants 校验两种测试形态：常规画面与近黑画面。
func TestSimulateVMConsolePPMVariants(t *testing.T) {
	parsePPM := func(t *testing.T, encoded string) (header string, pixels []byte) {
		t.Helper()
		decoded, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
		if err != nil {
			t.Fatalf("base64 解码失败: %v", err)
		}
		if !strings.HasPrefix(string(decoded), "P6\n") {
			t.Fatalf("不是 P6 PPM: %q", decoded[:8])
		}
		parts := strings.SplitN(string(decoded), "\n", 4)
		if len(parts) != 4 {
			t.Fatalf("PPM 结构异常: %q", decoded)
		}
		return parts[1] + " maxval=" + parts[2], []byte(parts[3])
	}

	normalClaims := vmConsoleTestClaims()
	stdout, _, matched := simulateVMConsoleCommand("base64 -w0 "+vmConsoleCapturePath(), normalClaims)
	if !matched {
		t.Fatal("常规 variant 未匹配")
	}
	dims, pixels := parsePPM(t, stdout)
	if dims != "8 6 maxval=255" {
		t.Fatalf("PPM 尺寸/最大像素值异常: %s", dims)
	}
	if len(pixels) != 8*6*3 {
		t.Fatalf("像素字节数异常: %d", len(pixels))
	}
	bright := 0
	for _, value := range pixels {
		if value > 24 {
			bright++
		}
	}
	if bright < len(pixels)/2 {
		t.Fatalf("常规画面应为有效亮画面，非近黑像素比例过低: %d/%d", bright, len(pixels))
	}

	nearBlackClaims := vmConsoleTestClaims()
	nearBlackClaims.FixtureVariant = "near-black-realistic"
	stdout, _, matched = simulateVMConsoleCommand("base64 -w0 "+vmConsoleCapturePath(), nearBlackClaims)
	if !matched {
		t.Fatal("近黑 variant 未匹配")
	}
	_, pixels = parsePPM(t, stdout)
	for _, value := range pixels {
		if value > 2 {
			t.Fatalf("近黑画面所有像素亮度应 ≤ 2，实际出现 %d", value)
		}
	}
}

// TestSSHExecServesVMConsoleFixedOperations 通过完整 SSH 链路验证仿真分支：
// 固定操作成功、输出合法，变形命令被严格路由 fail-closed 拒绝。
func TestSSHExecServesVMConsoleFixedOperations(t *testing.T) {
	router, err := fixture.Parse(serverManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(privateKey)
	if err != nil {
		t.Fatal(err)
	}
	secret := []byte("0123456789abcdef0123456789abcdef")
	srv, err := New(Config{ListenAddress: "127.0.0.1:0", HostSigner: signer, LeaseSecret: secret, Router: router, Workers: 1, QueueSize: 4, MaxOutputBytes: 4096, LeaseIssuer: "hci-platform", LeaseAudience: "hci-sim", Metrics: &metrics.Metrics{}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = srv.Serve(ctx) }()
	deadline := time.Now().Add(2 * time.Second)
	for srv.Addr() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if srv.Addr() == "" {
		t.Fatal("SSH server 未开始监听")
	}

	claims := testClaims(router, time.Now())
	claims.VirtualNodeID = vmConsoleTestNode
	token, err := lease.Sign(secret, claims)
	if err != nil {
		t.Fatal(err)
	}
	client, err := ssh.Dial("tcp", srv.Addr(), &ssh.ClientConfig{User: "sim", Auth: []ssh.AuthMethod{ssh.Password(token)}, HostKeyCallback: ssh.InsecureIgnoreHostKey(), Timeout: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	// screendump 与 sendkey down：成功且无输出
	for _, command := range []string{
		vmConsoleScreendumpCommand(),
		"vtpsh create /nodes/" + vmConsoleTestNode + "/qemu/" + vmConsoleTestVMID + "/monitor --command 'sendkey down'",
		"rm -f " + vmConsoleCapturePath(),
	} {
		session, sessionErr := client.NewSession()
		if sessionErr != nil {
			t.Fatal(sessionErr)
		}
		output, runErr := session.CombinedOutput(command)
		if runErr != nil || string(output) != "" {
			t.Fatalf("固定操作应成功且无输出: command=%q output=%q err=%v", command, output, runErr)
		}
	}

	// base64 读取：返回可解码的 P6 PPM
	session, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	output, err := session.CombinedOutput("base64 -w0 " + vmConsoleCapturePath())
	if err != nil {
		t.Fatalf("base64 读取失败: %v", err)
	}
	decoded, decodeErr := base64.StdEncoding.DecodeString(strings.TrimSpace(string(output)))
	if decodeErr != nil || !strings.HasPrefix(string(decoded), "P6\n") {
		t.Fatalf("base64 输出不是合法 P6 PPM: %q err=%v", output, decodeErr)
	}

	// 变形命令落回严格路由 → fixture_not_found fail-closed
	session, err = client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	output, err = session.CombinedOutput("vtpsh create /nodes/" + vmConsoleTestNode + "/qemu/" + vmConsoleTestVMID + "/monitor --command 'sendkey up'")
	exitErr, ok := err.(*ssh.ExitError)
	if !ok || exitErr.ExitStatus() != exitFixtureNotFound || !strings.Contains(string(output), "fixture_not_found") {
		t.Fatalf("变形 Monitor 命令未被 fail-closed: output=%q err=%v", output, err)
	}
}
