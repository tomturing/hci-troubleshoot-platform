package main

import (
	"testing"
	"time"
)

// 健康链路上保活探测必须成功：服务端对未知全局请求回 REQUEST_FAILURE，
// 对客户端而言仍是「链路活着」的证据，不能误判为失效。
func TestKeepAliveProbeSucceedsOnHealthySession(t *testing.T) {
	host, port := startFakeSSHServer(t, func(string) (string, int) { return "", 0 })
	session := newFakeSSHSession(t, host, port)

	if err := session.probeKeepAlive(); err != nil {
		t.Fatalf("健康链路保活探测失败: %v", err)
	}

	session.startKeepAlive(20 * time.Millisecond)
	time.Sleep(150 * time.Millisecond)
	if session.isClosed() {
		t.Fatal("健康会话被保活误杀")
	}
}

// 链路断开后保活必须识别出来并关闭会话：
// 关闭会让 pty 读循环 EOF，从而走既有 onExit 清理链路（移除会话 + 通知前端）。
func TestKeepAliveClosesSessionOnBrokenLink(t *testing.T) {
	host, port := startFakeSSHServer(t, func(string) (string, int) { return "", 0 })
	session := newFakeSSHSession(t, host, port)

	session.mu.Lock()
	client := session.client
	session.mu.Unlock()
	if client == nil {
		t.Fatal("会话未建立")
	}
	_ = client.Close()

	session.startKeepAlive(20 * time.Millisecond)

	deadline := time.Now().Add(3 * time.Second)
	for !session.isClosed() {
		if time.Now().After(deadline) {
			t.Fatal("断链后保活未关闭会话")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// 间隔 <=0 表示关闭应用层探测（仅保留 TCP keepalive），不能在关闭配置下空转。
func TestKeepAliveIntervalFromEnv(t *testing.T) {
	t.Setenv("HCI_BRIDGE_SSH_KEEPALIVE_SECONDS", "0")
	if got := sshKeepAliveInterval(); got != 0 {
		t.Fatalf("=0 应关闭应用层探测，实际 %v", got)
	}
	t.Setenv("HCI_BRIDGE_SSH_KEEPALIVE_SECONDS", "45")
	if got := sshKeepAliveInterval(); got != 45*time.Second {
		t.Fatalf("期望 45s，实际 %v", got)
	}
}
