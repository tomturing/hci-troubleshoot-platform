package main

import (
	"testing"
	"time"
)

// TestResolveInputSessionNodeScopedKey 复现现场缺陷：
// ssh_connect 带 node_ip 后会话键是 caseID@nodeIP，而输入报文只带 case_id。
// 历史实现只查 caseID，查不到就静默丢弃 —— 表现为「打字回车毫无反应」。
func TestResolveInputSessionNodeScopedKey(t *testing.T) {
	scoped := &SSHSession{}
	bridge := &Bridge{sessions: map[string]*SSHSession{
		"Q2026101401683@172.28.24.129": scoped,
	}}
	owned := newOwnedSessionTracker()

	got, key := bridge.resolveInputSession(InMessage{CaseID: "Q2026101401683"}, "", owned)
	if got != scoped || key != "Q2026101401683@172.28.24.129" {
		t.Fatalf("应回退命中带 IP 的会话键，实际 got=%v key=%q", got, key)
	}

	got, key = bridge.resolveInputSession(InMessage{CaseID: "Q2026101401683", NodeIP: "172.28.24.129"}, "", owned)
	if got != scoped || key != "Q2026101401683@172.28.24.129" {
		t.Fatalf("带 node_ip 时应精确命中，实际 got=%v key=%q", got, key)
	}

	// 无会话时必须返回 nil，由调用方留痕并提示，而不是静默
	empty := &Bridge{sessions: map[string]*SSHSession{}}
	if got, _ := empty.resolveInputSession(InMessage{CaseID: "Q2026101401683"}, "", newOwnedSessionTracker()); got != nil {
		t.Fatal("无会话时不得凭空命中")
	}
}

// TestResolveInputSessionPrefersActiveKey 多节点同工单时只认本连接刚建立的会话，不跨工单串台。
func TestResolveInputSessionPrefersActiveKey(t *testing.T) {
	first := &SSHSession{}
	second := &SSHSession{}
	bridge := &Bridge{sessions: map[string]*SSHSession{
		"Q1@10.0.0.1": first,
		"Q1@10.0.0.2": second,
	}}
	owned := newOwnedSessionTracker()
	owned.set("Q1@10.0.0.1", first)
	owned.set("Q1@10.0.0.2", second)

	got, key := bridge.resolveInputSession(InMessage{CaseID: "Q1"}, "Q1@10.0.0.2", owned)
	if got != second || key != "Q1@10.0.0.2" {
		t.Fatalf("应命中本连接活动会话，实际 got=%v key=%q", got, key)
	}

	if got, _ := bridge.resolveInputSession(InMessage{CaseID: "Q2"}, "Q1@10.0.0.2", owned); got != nil {
		t.Fatal("跨工单不得复用活动会话，否则输入会打到别的节点")
	}
}

// TestReplayIncludesEmptyCaseLifecycleEvents 覆盖实测丢日志的根因：
// bridge.startup / connected 等事件没有 case_id，且发生在订阅之前，
// 只按 caseID 回放会把它们全部漏掉。
func TestReplayIncludesEmptyCaseLifecycleEvents(t *testing.T) {
	now := time.Now().UTC()
	hub := &LogHub{ring: []logEntry{
		{Seq: 1, CaseID: "", Timestamp: now.Add(-1 * time.Minute).Format(time.RFC3339Nano), Event: "bridge.startup"},
		{Seq: 2, CaseID: "", Timestamp: now.Add(-30 * time.Minute).Format(time.RFC3339Nano), Event: "bridge.startup"},
		{Seq: 3, CaseID: "Q1", Timestamp: now.Format(time.RFC3339Nano), Event: "ssh.connected"},
		{Seq: 4, CaseID: "Q2", Timestamp: now.Format(time.RFC3339Nano), Event: "ssh.connected"},
	}}

	replay := hub.replayForCaseLocked(&bridgeSubscriber{bornSeq: 100}, "Q1", now)
	if len(replay) != 2 || replay[0].Seq != 1 || replay[1].Seq != 3 {
		t.Fatalf("应回放「窗口内的空 case 事件 + 本工单事件」，实际 %+v", replay)
	}
	// 窗口外的历史事件不重复回放，避免重连一次塞回一堆陈旧日志
	for _, e := range replay {
		if e.Seq == 2 {
			t.Fatal("超出回放窗口的空 case 事件不应回放")
		}
	}

	if got := hub.replayForCaseLocked(&bridgeSubscriber{}, "", now); got != nil {
		t.Fatal("没有工单归属时不得回放")
	}
}

// TestReplaySkipsEventsProducedAfterSubscribe 订阅之后产生的事件已实时推送，不得再回放。
// 注意判定依据是订阅水位 bornSeq，而不是"最大已投递 seq"——后者会把订阅前更早的事件
// 一并误判为已投递（端到端验证时确实复现过：bridge.startup 被吞掉）。
func TestReplaySkipsEventsProducedAfterSubscribe(t *testing.T) {
	now := time.Now().UTC()
	hub := &LogHub{ring: []logEntry{
		{Seq: 1, CaseID: "", Timestamp: now.Format(time.RFC3339Nano), Event: "bridge.startup"},
		{Seq: 3, CaseID: "Q1", Timestamp: now.Format(time.RFC3339Nano), Event: "ssh.connected"},
		{Seq: 5, CaseID: "Q1", Timestamp: now.Format(time.RFC3339Nano), Event: "ssh.output"},
	}}

	// bornSeq=4：订阅发生在 seq=4 之前，因此 1/3 属于"订阅前"需要回放，5 已实时推送过
	replay := hub.replayForCaseLocked(&bridgeSubscriber{bornSeq: 4}, "Q1", now)
	if len(replay) != 2 || replay[0].Seq != 1 || replay[1].Seq != 3 {
		t.Fatalf("只应回放订阅前的事件，实际 %+v", replay)
	}
}

// TestReplayOnlyOnce 同一连接多次 setCase（ssh_connect → resume）只增量回放。
func TestReplayOnlyOnce(t *testing.T) {
	now := time.Now().UTC()
	hub := &LogHub{ring: []logEntry{
		{Seq: 1, CaseID: "", Timestamp: now.Format(time.RFC3339Nano), Event: "bridge.startup"},
		{Seq: 3, CaseID: "Q1", Timestamp: now.Format(time.RFC3339Nano), Event: "ssh.connected"},
	}}
	sub := &bridgeSubscriber{bornSeq: 10}

	if first := hub.replayForCaseLocked(sub, "Q1", now); len(first) != 2 {
		t.Fatalf("首次应回放全部订阅前事件，实际 %+v", first)
	}
	if second := hub.replayForCaseLocked(sub, "Q1", now); len(second) != 0 {
		t.Fatalf("同一批事件不得重复回放，实际 %+v", second)
	}
}

// TestSessionKeyCase 会话键解析必须只切第一个 @，避免主机名里带 @ 时误判。
func TestSessionKeyCase(t *testing.T) {
	cases := map[string]string{
		"Q1":                     "Q1",
		"Q1@172.28.24.129":       "Q1",
		"Q1@host@weird.name":     "Q1",
		"terminal-only@10.1.1.1": "terminal-only",
	}
	for key, want := range cases {
		if got := sessionKeyCase(key); got != want {
			t.Fatalf("sessionKeyCase(%q)=%q，期望 %q", key, got, want)
		}
	}
}
