//go:build windows

package main

import (
	"sync/atomic"
	"syscall"
)

// Windows 控制台控制事件码（WinCon.h）。
// Go runtime 只把前两个映射成 os.Interrupt（日志 signal=interrupt），
// 后三个统一映射成 syscall.SIGTERM（日志 signal=terminated），
// 因此只靠 signal 字段无法区分"用户关了窗口"与"注销/关机"。
const (
	consoleCtrlCEvent        = 0
	consoleCtrlBreakEvent    = 1
	consoleCtrlCloseEvent    = 2
	consoleCtrlLogoffEvent   = 5
	consoleCtrlShutdownEvent = 6
)

var (
	kernel32DLL               = syscall.NewLazyDLL("kernel32.dll")
	procSetConsoleCtrlHandler = kernel32DLL.NewProc("SetConsoleCtrlHandler")
	consoleCtrlLastEvent      atomic.Uint32
)

// registerConsoleCtrlProbe 注册一个只读的控制台事件探针，把事件码如实记录下来
// 供 bridge.shutdown 事件自证退出原因。
//
// 关键约束：处理器必须返回 FALSE(0)，把事件继续交给 Go runtime 自己的处理器；
// 若返回 TRUE，runtime 收不到事件、SIGINT/SIGTERM 不再投递，优雅退出链路会整体失效。
// Windows 按"后注册先调用"顺序执行处理器，所以本探针一定先于 runtime 拿到事件码，
// 信号 goroutine 读到的 console_event 必然是本次退出对应的那个。
func registerConsoleCtrlProbe() {
	callback := syscall.NewCallback(func(ctrlType uintptr) uintptr {
		consoleCtrlLastEvent.Store(uint32(ctrlType))
		return 0 // FALSE：继续交给 Go runtime 的处理器
	})
	// 注册失败不影响主流程：只是少一个诊断字段，信号与优雅退出仍由 runtime 负责。
	_, _, _ = procSetConsoleCtrlHandler.Call(callback, 1)
}

// consoleCtrlEventName 把事件码翻译成可读名；未命中返回空串。
func consoleCtrlEventName(event uint32) string {
	switch event {
	case consoleCtrlCEvent:
		return "ctrl_c"
	case consoleCtrlBreakEvent:
		return "ctrl_break"
	case consoleCtrlCloseEvent:
		return "ctrl_close"
	case consoleCtrlLogoffEvent:
		return "ctrl_logoff"
	case consoleCtrlShutdownEvent:
		return "ctrl_shutdown"
	default:
		return ""
	}
}

// lastConsoleCtrlEvent 返回本次进程生命周期内最后一次控制台事件名（无则空串）。
func lastConsoleCtrlEvent() string {
	return consoleCtrlEventName(consoleCtrlLastEvent.Load())
}
