//go:build !windows

package main

import "testing"

// 非 Windows 平台不注册控制台探针：console_event 必须保持缺省，
// 否则 bridge.shutdown 事件会带上一个语义不存在的字段。
func TestConsoleCtrlProbeUnavailableOnNonWindows(t *testing.T) {
	registerConsoleCtrlProbe() // 空实现，调用不得 panic
	if got := lastConsoleCtrlEvent(); got != "" {
		t.Fatalf("非 Windows 平台 console_event 应为空，实际 %q", got)
	}
}
