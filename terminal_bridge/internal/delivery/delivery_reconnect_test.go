package delivery

import (
	"io"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"testing"
	"time"
)

// TestDeliverySessionForLiveSession 守护：已有会话时直接复用，不触发自动重连。
func TestDeliverySessionForLiveSession(t *testing.T) {
	b := session.NewHub()
	sess := &sshx.SSHSession{CaseID: "case-x", CreatedAt: time.Now()}
	b.Set(session.SessionKey("case-x", "10.0.0.1"), sess)
	wrapped := sshx.NewSSHRunner(sess)
	r := &deliveryRunner{hub: b, reconnect: staticReconnector{wrap: wrapped}}
	job := &deliveryJob{CaseID: "case-x", NodeIP: "10.0.0.1", Phase: phasePulling}
	if got := r.sessionFor(job); got != NodeSession(wrapped) {
		t.Fatalf("期望复用已有会话，得到 %v", got)
	}
}

// TestDeliverySessionForNoAuthReturnsNil 守护：没有 lastAuth、也没有会话时，
// 不尝试连接、不 panic，返回 nil（由调用方转为 needs_reauth，走人工重连）。
// 这是「裸」交付的正确退路——不能因为没有节点就假装能自动恢复。
func TestDeliverySessionForNoAuthReturnsNil(t *testing.T) {
	r := &deliveryRunner{hub: session.NewHub(), reconnect: noopReconnector{}}
	job := &deliveryJob{CaseID: "case-x", NodeIP: "10.0.0.1", Phase: phasePulling}
	if got := r.sessionFor(job); got != nil {
		t.Fatalf("期望无会话返回 nil，得到 %v", got)
	}
}

// noopReconnector 是 Reconnector 的空实现：这些用例只验证"不触发重连"的路径。
type noopReconnector struct{}

func (noopReconnector) Wrap(key string) NodeSession { return nil }

func (noopReconnector) ReconnectDeliveryNode(caseID, nodeIP, connID string) (NodeSession, error) {
	return nil, errNoAuthForTest
}

var errNoAuthForTest = io.EOF

// staticReconnector 固定返回一个已适配的节点端口，用于验证"复用已有会话"的路径。
type staticReconnector struct {
	wrap NodeSession
}

func (s staticReconnector) Wrap(key string) NodeSession { return s.wrap }

func (s staticReconnector) ReconnectDeliveryNode(caseID, nodeIP, connID string) (NodeSession, error) {
	return nil, errNoAuthForTest
}
