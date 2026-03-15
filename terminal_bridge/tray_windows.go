//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

// setSysProcAttr 设置 Windows 进程属性，隐藏 SSH 子进程的控制台窗口
func setSysProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: 0x08000000, // CREATE_NO_WINDOW
	}
}

// runTray Windows 下静默后台运行，无托盘图标
// 客户可通过任务管理器结束进程
func runTray() {
	// 静默运行，不显示任何窗口
	// 如需托盘图标，后续可引入 github.com/getlantern/systray
}
