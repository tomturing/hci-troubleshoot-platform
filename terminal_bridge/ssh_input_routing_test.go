package main

import (
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/sshx"
	"testing"
)

// TestResolveInputSessionNodeScopedKey 复现现场缺陷：
// ssh_connect 带 node_ip 后会话键是 caseID@nodeIP，而输入报文只带 case_id。
// 历史实现只查 caseID，查不到就静默丢弃 —— 表现为「打字回车毫无反应」。
func TestResolveInputSessionNodeScopedKey(t *testing.T) {
	scoped := &sshx.SSHSession{}
	bridge := &Bridge{sessions: map[string]*sshx.SSHSession{
		"Q2026101401683@192.0.2.21": scoped,
	}}
	owned := newOwnedSessionTracker()

	got, key := bridge.resolveInputSession(models.InMessage{CaseID: "Q2026101401683"}, "", owned)
	if got != scoped || key != "Q2026101401683@192.0.2.21" {
		t.Fatalf("应回退命中带 IP 的会话键，实际 got=%v key=%q", got, key)
	}

	got, key = bridge.resolveInputSession(models.InMessage{CaseID: "Q2026101401683", NodeIP: "192.0.2.21"}, "", owned)
	if got != scoped || key != "Q2026101401683@192.0.2.21" {
		t.Fatalf("带 node_ip 时应精确命中，实际 got=%v key=%q", got, key)
	}

	// 无会话时必须返回 nil，由调用方留痕并提示，而不是静默
	empty := &Bridge{sessions: map[string]*sshx.SSHSession{}}
	if got, _ := empty.resolveInputSession(models.InMessage{CaseID: "Q2026101401683"}, "", newOwnedSessionTracker()); got != nil {
		t.Fatal("无会话时不得凭空命中")
	}
}

// TestResolveInputSessionPrefersActiveKey 多节点同工单时只认本连接刚建立的会话，不跨工单串台。
func TestResolveInputSessionPrefersActiveKey(t *testing.T) {
	first := &sshx.SSHSession{}
	second := &sshx.SSHSession{}
	bridge := &Bridge{sessions: map[string]*sshx.SSHSession{
		"Q1@10.0.0.1": first,
		"Q1@10.0.0.2": second,
	}}
	owned := newOwnedSessionTracker()
	owned.set("Q1@10.0.0.1", first)
	owned.set("Q1@10.0.0.2", second)

	got, key := bridge.resolveInputSession(models.InMessage{CaseID: "Q1"}, "Q1@10.0.0.2", owned)
	if got != second || key != "Q1@10.0.0.2" {
		t.Fatalf("应命中本连接活动会话，实际 got=%v key=%q", got, key)
	}

	if got, _ := bridge.resolveInputSession(models.InMessage{CaseID: "Q2"}, "Q1@10.0.0.2", owned); got != nil {
		t.Fatal("跨工单不得复用活动会话，否则输入会打到别的节点")
	}
}

// TestSessionKeyCase 会话键解析必须只切第一个 @，避免主机名里带 @ 时误判。
func TestSessionKeyCase(t *testing.T) {
	cases := map[string]string{
		"Q1":                     "Q1",
		"Q1@192.0.2.21":          "Q1",
		"Q1@host@weird.name":     "Q1",
		"terminal-only@10.1.1.1": "terminal-only",
	}
	for key, want := range cases {
		if got := sessionKeyCase(key); got != want {
			t.Fatalf("sessionKeyCase(%q)=%q，期望 %q", key, got, want)
		}
	}
}
