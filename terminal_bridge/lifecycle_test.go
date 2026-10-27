package main

// 生命周期事件的单测。
//
// 这些用例守护的是一个"排障契约"：任何会话消失都必须能在日志里回答
// 「谁销毁的、为什么、活了多久、当时还剩几条」。
// 2026-10-27 现场只能看到 active_sessions=0，就是因为缺少这组事件。

import (
	"testing"
	"time"
)

// findLogEventSince 在全局 ring 里找指定事件（只看 seq 之后的新增条目）。
//
// 全局 logHub 会跨用例累积，必须用起始水位隔离，否则断言会命中别的用例写入的事件。
func findLogEventSince(t *testing.T, startSeq uint64, event string) *logEntry {
	t.Helper()
	logHub.mu.Lock()
	defer logHub.mu.Unlock()
	for i := len(logHub.ring) - 1; i >= 0; i-- {
		entry := logHub.ring[i]
		if entry.Seq <= startSeq {
			break
		}
		if entry.Event == event {
			return &entry
		}
	}
	return nil
}

func ringSeq() uint64 {
	logHub.mu.Lock()
	defer logHub.mu.Unlock()
	return logHub.seq
}

// 销毁会话必须同时完成三件事：摘表、关闭、留痕（含原因/行为者/存活时长）。
func TestDestroySessionEmitsLifecycleEvent(t *testing.T) {
	b := newBridge()
	key := "Q2026102700862@10.0.11.100"
	session := &SSHSession{
		caseID:    "Q2026102700862",
		address:   "10.0.11.100:22",
		done:      make(chan struct{}),
		ownerConn: "c1-1",
		createdAt: time.Now().Add(-90 * time.Second),
	}
	b.set(key, session)

	seq := ringSeq()
	if !b.destroySession(key, sessionReasonWSClosed, "c2-2") {
		t.Fatal("首次销毁应返回 true")
	}
	if b.get(key) != nil {
		t.Fatal("销毁后会话仍在表中")
	}
	if !session.isClosed() {
		t.Fatal("销毁后底层连接未关闭")
	}

	entry := findLogEventSince(t, seq, "session.destroyed")
	if entry == nil {
		t.Fatal("未记录 session.destroyed 事件")
	}
	if got := entry.Extra["reason"]; got != sessionReasonWSClosed {
		t.Fatalf("reason 期望 %s，实际 %v", sessionReasonWSClosed, got)
	}
	if got := entry.Extra["by_conn"]; got != "c2-2" {
		t.Fatalf("by_conn 期望 c2-2，实际 %v", got)
	}
	if got := entry.Extra["owner_conn"]; got != "c1-1" {
		t.Fatalf("owner_conn 期望 c1-1，实际 %v", got)
	}
	// 存活时长用于判断"是秒断还是长连接被清"，缺失就等于丢了时间线。
	if age, ok := entry.Extra["age_seconds"]; !ok || age.(int64) < 60 {
		t.Fatalf("age_seconds 异常: %v", age)
	}
	if entry.CaseID != "Q2026102700862" {
		t.Fatalf("事件必须带 case_id，实际 %q", entry.CaseID)
	}
}

// 重复销毁必须留下 destroy_miss：它证明清理链路被走了两次，
// 是判断"连坐清理 + 远端 EOF 双重清理"的唯一依据。
func TestDestroySessionMissIsLogged(t *testing.T) {
	b := newBridge()
	key := "Q1@10.0.0.1"
	seq := ringSeq()

	if b.destroySession(key, sessionReasonWSClosed, "c1-1") {
		t.Fatal("键不存在时应返回 false")
	}
	if findLogEventSince(t, seq, "session.destroy_miss") == nil {
		t.Fatal("销毁落空未留痕")
	}
	if findLogEventSince(t, seq, "session.destroyed") != nil {
		t.Fatal("落空不应记 destroyed")
	}
}

// 连接角色决定"探测连接"与"业务连接"能否区分，映射错了会把端口探测误判成事故。
func TestRoleOfMessageType(t *testing.T) {
	cases := map[string]string{
		"ssh_connect":  connRoleTerminal,
		"ssh_input":    connRoleTerminal,
		"log_collect":  connRolePanel,
		"acli_sync":    connRolePanel,
		"resume":       "",
		"ping":         "",
		"unknown_type": "",
	}
	for msgType, want := range cases {
		if got := roleOfMessageType(msgType); got != want {
			t.Fatalf("roleOfMessageType(%q) 期望 %q，实际 %q", msgType, want, got)
		}
	}
}

// 键拆解必须区分「带 node_ip」与「建单流程退化的 caseID 键」：
// 后者 node_ip 只能为空，不能猜。
func TestSessionKeyNode(t *testing.T) {
	if got := sessionKeyNode("Q1@10.0.11.100"); got != "10.0.11.100" {
		t.Fatalf("带 IP 的键拆解失败: %q", got)
	}
	if got := sessionKeyNode("ssh-create-temp"); got != "" {
		t.Fatalf("退化键应返回空 node，实际 %q", got)
	}
}
