package main

// 交付长任务与 SSH 会话生命周期的交互（2026-10-28 现场：12.8 GiB 包永远搬不完）。
//
// 两个约束叠加才致命：
//  1. HCI 交互式 shell 有 TMOUT=600，10 分钟无输入即退出；
//  2. 拉取 12.8 GiB @ ~10 MB/s 需要 20 分钟以上。
//
// 因此"shell 退出 == 连接失效"这个历史假设必然把交付腰斩。这里钉住两件事：
// 长任务期间会话不被空闲租约回收；长任务结束后回收能力恢复（不能变成永久泄漏）。

import (
	"testing"
	"time"
)

func TestReaperKeepsSessionWithLongTask(t *testing.T) {
	bridge := newBridge()
	session := &SSHSession{done: make(chan struct{}), createdAt: time.Now()}
	key := "Q2026102801014@192.168.1.20"
	bridge.set(key, session)
	// 浏览器早已关闭是大包交付的常态：refs 归零后进入空闲租约。
	bridge.releaseSession(key, "conn-1")

	session.beginLongTask()
	if reaped := bridge.reapIdleSessions(time.Nanosecond); reaped != 0 {
		t.Fatalf("长任务进行中的会话不应被空闲租约回收，实际回收 %d 条", reaped)
	}
	if bridge.get(key) == nil {
		t.Fatal("长任务会话被误删，正在进行的拉取会被腰斩")
	}

	// 长任务结束后必须恢复回收，否则孤儿会话永久占用远端 shell。
	session.endLongTask()
	if reaped := bridge.reapIdleSessions(time.Nanosecond); reaped != 1 {
		t.Fatalf("长任务结束后应恢复常规回收，实际回收 %d 条", reaped)
	}
}

func TestShellHeartbeatStopIsIdempotent(t *testing.T) {
	// 心跳 goroutine 必须在 stop 后可重复调用而不 panic（defer 与显式调用并存）。
	session := &SSHSession{done: make(chan struct{}), createdAt: time.Now()}
	stop := session.startShellHeartbeat("Q2026102801014")
	stop()
	stop()
	close(session.done)
}
