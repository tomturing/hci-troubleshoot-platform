package session

import (
	"testing"

	"terminal_bridge/internal/sshx"
)

// NodeFromKey 只切第一个 @：主机名里带 @ 时不能把后半段当节点 IP。
func TestNodeFromKey(t *testing.T) {
	cases := map[string]string{
		"Q1@10.0.11.100":  "10.0.11.100",
		"Q1":              "",    // 建单集成流程的会话键退化为 caseID
		"ssh-create-temp": "",    // 临时键不含节点
		"Q1@a@b":          "a@b", // 只切第一个 @
		"@10.0.0.1":       "10.0.0.1",
		"":                "",
	}
	for key, want := range cases {
		if got := NodeFromKey(key); got != want {
			t.Fatalf("NodeFromKey(%q)=%q，期望 %q", key, got, want)
		}
	}
}

// 会话建立事件必须把「谁建的」与「何时建」写进会话对象：
// 这两项是事后区分「自动建连 / 用户建连」和计算存活时长的唯一依据。
func TestLogCreatedSetsOwnerAndCreatedAt(t *testing.T) {
	s := &sshx.SSHSession{}
	LogCreated(s, "Q1@10.0.0.1", "c-42", "custom-ui", "ssh_connect")
	if s.OwnerConn != "c-42" {
		t.Fatalf("OwnerConn=%q，期望 c-42", s.OwnerConn)
	}
	if s.CreatedAt.IsZero() {
		t.Fatal("CreatedAt 应被设置为当前时刻")
	}
}

// nil 会话不得 panic：建链失败路径上有调用方可能已持有 nil。
func TestLogCreatedNilSafe(t *testing.T) {
	LogCreated(nil, "Q1@10.0.0.1", "c-1", "custom-ui", "ssh_connect")
}
