package sshx_test

// sshx 保活与业务活跃窗口的契约测试。
//
// 为什么这两件事要一起测：它们解决的是同一个现场问题——HCI 的 TMOUT=600 与会话
// 空闲租约都会在"看起来还连着"的时候把连接收走。保活负责让链路上一直有字节，
// busy grace 负责让空闲回收器不动正在跑采集的会话；任何一个失守，表现都是
// 「progress 全部 session_missing」（2026-10-28 现场）。

import (
	"os"
	"testing"
	"time"

	"terminal_bridge/internal/sshx"

	"golang.org/x/crypto/ssh"
)

func TestSshKeepAliveIntervalDefault(t *testing.T) {
	t.Setenv("HCI_BRIDGE_SSH_KEEPALIVE_SECONDS", "")
	if got := sshx.SshKeepAliveInterval(); got != 30*time.Second {
		t.Fatalf("默认保活间隔应为 30s，实际 %s", got)
	}
}

func TestSshKeepAliveIntervalEnvOverride(t *testing.T) {
	cases := []struct {
		env  string
		want time.Duration
	}{
		{"60", 60 * time.Second},
		{"5", 5 * time.Second},
		{"0", 0},                 // 显式关闭应用层探测（仅保留 TCP keepalive）
		{"-1", -1 * time.Second}, // 与 0 同义，但不能被当成 1 秒
		{"abc", 30 * time.Second},
	}
	for _, c := range cases {
		t.Setenv("HCI_BRIDGE_SSH_KEEPALIVE_SECONDS", c.env)
		if got := sshx.SshKeepAliveInterval(); got != c.want {
			t.Fatalf("env=%q 时应为 %s，实际 %s", c.env, c.want, got)
		}
	}
}

// TestProbeKeepAliveWithoutClient 会话已关闭时必须立刻报错，不能等 10s 探测超时。
func TestProbeKeepAliveWithoutClient(t *testing.T) {
	s := &sshx.SSHSession{Done: make(chan struct{})}
	err := s.ProbeKeepAlive()
	if err == nil {
		t.Fatal("没有 ssh client 时探测必须失败")
	}
	if err.Error() != "ssh client 已关闭" {
		t.Fatalf("错误文案应指明原因，实际 %q", err.Error())
	}
}

// TestStartKeepAliveShortCircuitsForTestSession 手工构造的会话（Done==nil）不得启动 goroutine。
//
// 真实会话的 Done 由连接生命周期创建；测试/工具里手工构造的会话没有退出信号，
// 启动保活就是一个永不被回收的 goroutine（且会周期性探测 nil client）。
func TestStartKeepAliveShortCircuitsForTestSession(t *testing.T) {
	s := &sshx.SSHSession{}
	// 不应 panic，也不应留下 goroutine；这里用极短间隔确保即使误启动也会在测试内暴露。
	s.StartKeepAlive(time.Nanosecond)
	s.StartKeepAlive(0)
	s.StartKeepAlive(-1)
}

// TestSessionBusyGraceEnv 业务活跃窗口必须可配、且 <=0 表示关闭保护。
//
// 关闭不是禁用回收，而是退回"只看长任务计数"的旧行为——有些现场不希望
// 15 分钟的宽限期掩盖真实空闲。
func TestSessionBusyGraceEnv(t *testing.T) {
	t.Setenv("HCI_BRIDGE_SESSION_BUSY_GRACE_SECONDS", "")
	if got := sshx.SessionBusyGrace(); got != sshx.DefaultSessionBusyGrace {
		t.Fatalf("默认窗口应为 %s，实际 %s", sshx.DefaultSessionBusyGrace, got)
	}
	t.Setenv("HCI_BRIDGE_SESSION_BUSY_GRACE_SECONDS", "60")
	if got := sshx.SessionBusyGrace(); got != 60*time.Second {
		t.Fatalf("env 覆盖后应为 60s，实际 %s", got)
	}
	t.Setenv("HCI_BRIDGE_SESSION_BUSY_GRACE_SECONDS", "0")
	if got := sshx.SessionBusyGrace(); got != 0 {
		t.Fatalf("0 表示关闭保护，实际 %s", got)
	}
}

// TestPtyNormalizeIsIdempotent CRLF 归一化必须幂等，且不碰孤立 \r。
func TestPtyNormalizeIsIdempotent(t *testing.T) {
	cases := []struct{ in, want string }{
		{"a\r\nb", "a\nb"},
		{"a\nb", "a\nb"},
		{"a\rb", "a\rb"}, // 孤立 \r 不是换行转换的产物，保留原样
		{"", ""},
	}
	for _, c := range cases {
		once := sshx.NormalizePtyOutput(c.in)
		if once != c.want {
			t.Fatalf("NormalizePtyOutput(%q)=%q 期望 %q", c.in, once, c.want)
		}
		if twice := sshx.NormalizePtyOutput(once); twice != once {
			t.Fatalf("归一化不幂等：%q -> %q -> %q", c.in, once, twice)
		}
	}
}

// TestPtyTerminalModesDisableEchoAndOnlcr 回显与换行转换必须在协议层关掉。
//
// 只靠解析侧去 \r 不够：回显会把用户输入混进 stdout，ONLCR 会让每一行都变 CRLF，
// 后者会把 base64 回读校验与 cmp 保真校验全部打偏。
func TestPtyTerminalModesDisableEchoAndOnlcr(t *testing.T) {
	modes := sshx.PtyTerminalModes()
	for name, opcode := range map[string]uint8{
		"ECHO":  ssh.ECHO,
		"OPOST": ssh.OPOST,
		"ONLCR": ssh.ONLCR,
	} {
		if v, ok := modes[opcode]; !ok || v != 0 {
			t.Fatalf("终端模式 %s 必须显式置 0（避免依赖默认值与 sshd 差异）", name)
		}
	}
}

// TestRunCommandPtyRequiresSession 无会话时必须是可识别的错误，而不是 panic。
func TestRunCommandPtyRequiresSession(t *testing.T) {
	var s *sshx.SSHSession
	out, code, err := s.RunCommandPty(t.Context(), "true", time.Second)
	if err == nil || out != "" || code != -1 {
		t.Fatalf("无会话时应返回 (\"\", -1, err)，实际 (%q,%d,%v)", out, code, err)
	}
	empty := &sshx.SSHSession{}
	if _, _, err := empty.RunCommandPty(t.Context(), "true", time.Second); err == nil {
		t.Fatal("Client 为 nil 的会话也必须报错")
	}
}

func TestMain(m *testing.M) {
	// 防止宿主环境的保活配置影响断言（其余用例逐个 t.Setenv）。
	_ = os.Unsetenv("HCI_BRIDGE_SSH_KEEPALIVE_SECONDS")
	_ = os.Unsetenv("HCI_BRIDGE_SESSION_BUSY_GRACE_SECONDS")
	os.Exit(m.Run())
}
