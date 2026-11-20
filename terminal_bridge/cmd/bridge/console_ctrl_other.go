//go:build !windows

package main

// 非 Windows 平台没有"控制台控制事件"这一层：退出原因已由信号本身表达
// （SIGINT/SIGTERM/SIGHUP），无需额外探针，bridge.shutdown 的 console_event 保持缺省。
func registerConsoleCtrlProbe() {}

// lastConsoleCtrlEvent 在非 Windows 平台恒为空串。
func lastConsoleCtrlEvent() string { return "" }
