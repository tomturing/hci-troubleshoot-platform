package sshx

import (
	"testing"
	"time"
)

// 心跳必须让路给用户输入：NUL 在 bash 里等价 set-mark，但落进 vi/less/top
// 这类前台程序会变成可见/可感知的干扰。
func TestShouldSkipHeartbeatQuietWindow(t *testing.T) {
	now := time.Now().UnixNano()

	cases := []struct {
		name      string
		lastInput int64
		want      bool
	}{
		{"从未输入过", 0, false},
		{"刚敲过 1 秒", now - int64(time.Second), true},
		{"窗口边界内 29.9 秒", now - int64(29900*time.Millisecond), true},
		{"刚好越过窗口 30 秒", now - int64(shellHeartbeatQuietWindow), false},
		{"很久没输入 10 分钟", now - int64(10*time.Minute), false},
		// 时钟回拨：差值为负时按"刚输入过"处理，宁可少注入一次。
		{"时钟回拨（打戳在未来）", now + int64(time.Second), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := shouldSkipHeartbeat(tc.lastInput, now); got != tc.want {
				t.Fatalf("shouldSkipHeartbeat(%d) = %v，期望 %v", tc.lastInput, got, tc.want)
			}
		})
	}
}

// 心跳间隔必须显著小于 TMOUT=600，否则"跳过一轮"就会把 shell 送进超时窗口。
func TestHeartbeatIntervalLeavesTMOUTMargin(t *testing.T) {
	const tmoTimeout = 600 * time.Second
	if shellHeartbeatInterval+shellHeartbeatQuietWindow >= tmoTimeout {
		t.Fatalf("心跳间隔 %v + 静默窗口 %v 必须小于 TMOUT %v", shellHeartbeatInterval, shellHeartbeatQuietWindow, tmoTimeout)
	}
}
