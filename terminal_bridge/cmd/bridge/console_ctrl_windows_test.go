//go:build windows

package main

import "testing"

// 事件码到名字的映射必须覆盖 SIGTERM 的三种来源，否则「用户关了窗口」与
// 「系统注销/关机」仍然无法区分（2026-11-20 现场只看到 terminated，无法定责）。
func TestConsoleCtrlEventName(t *testing.T) {
	cases := []struct {
		name  string
		event uint32
		want  string
	}{
		{"ctrl_c", consoleCtrlCEvent, "ctrl_c"},
		{"ctrl_break", consoleCtrlBreakEvent, "ctrl_break"},
		{"ctrl_close", consoleCtrlCloseEvent, "ctrl_close"},
		{"ctrl_logoff", consoleCtrlLogoffEvent, "ctrl_logoff"},
		{"ctrl_shutdown", consoleCtrlShutdownEvent, "ctrl_shutdown"},
		{"unknown", 99, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := consoleCtrlEventName(tc.event); got != tc.want {
				t.Fatalf("consoleCtrlEventName(%d)=%q, 期望 %q", tc.event, got, tc.want)
			}
		})
	}
}

// 探针注册本身不得让进程崩溃（SetConsoleCtrlHandler 在无控制台的窗口化进程上会失败，
// 那属于可接受的降级：只是少一个诊断字段）。
func TestRegisterConsoleCtrlProbeDoesNotPanic(t *testing.T) {
	registerConsoleCtrlProbe()
}
