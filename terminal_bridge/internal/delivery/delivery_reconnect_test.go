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
	wrapped := liveNodeSession{}
	r := &deliveryRunner{hub: b, reconnect: staticReconnector{wrap: wrapped}}
	job := &deliveryJob{CaseID: "case-x", NodeIP: "10.0.0.1", Phase: phasePulling}
	if got := r.sessionFor(job); got != NodeSession(wrapped) {
		t.Fatalf("期望复用已有会话，得到 %v", got)
	}
}

// TestDeliverySessionRejectsOnlySessionFallback 守护：本工单只剩"另一节点的唯一会话"时，
// 交付不得拿它执行命令（会把 acli 命令发到错误的机器），必须转按节点自动重连。
//
// 现场形态见 Q2026112500592：.35 被 remote_eof 销毁后，ResolveForNode 的
// only_session_for_case 兜底返回 .38 的会话，连续 5 轮 acli progress 全部 no-session。
func TestDeliverySessionRejectsOnlySessionFallback(t *testing.T) {
	b := session.NewHub()
	// 只有 .38 的会话，作业要的是 .35。
	sess := &sshx.SSHSession{CaseID: "case-x", CreatedAt: time.Now()}
	b.Set(session.SessionKey("case-x", "10.0.0.38"), sess)
	wrapped := liveNodeSession{}
	r := &deliveryRunner{hub: b, reconnect: staticReconnector{wrap: wrapped}}
	job := &deliveryJob{CaseID: "case-x", NodeIP: "10.0.0.35", Phase: phaseWaitingCollect}
	if got := r.sessionFor(job); got != nil {
		t.Fatalf("不得跨节点兜底返回会话，得到 %v", got)
	}
}

// liveNodeSession 只实现 Alive：这些用例只验证会话解析与替换，不真正执行命令。
type liveNodeSession struct{ NodeSession }

func (liveNodeSession) Alive() bool { return true }

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
