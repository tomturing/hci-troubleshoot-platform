package obslog

import (
	"testing"
	"time"
)

// TestReplayIncludesEmptyCaseLifecycleEvents 覆盖实测丢日志的根因：
// bridge.startup / connected 等事件没有 case_id，且发生在订阅之前，
// 只按 caseID 回放会把它们全部漏掉。
func TestReplayIncludesEmptyCaseLifecycleEvents(t *testing.T) {
	now := time.Now().UTC()
	hub := &LogHub{Ring: []LogEntry{
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
	hub := &LogHub{Ring: []LogEntry{
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
	hub := &LogHub{Ring: []LogEntry{
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
