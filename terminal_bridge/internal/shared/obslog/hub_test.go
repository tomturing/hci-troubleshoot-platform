package obslog

import (
	"testing"

	"golang.org/x/net/websocket"
)

func TestLogHubPublishReturnsObservableFields(t *testing.T) {
	hub := &LogHub{
		cap:        10,
		subs:       make(map[*websocket.Conn]*bridgeSubscriber),
		InstanceID: "bridge-test-instance",
	}

	entry := hub.publish(LogEntry{Level: "INFO", Service: "terminal_bridge", Event: "test"})

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
