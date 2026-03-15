//go:build !windows

package main

import "os/exec"

// setSysProcAttr 非 Windows 平台无需特殊处理
func setSysProcAttr(cmd *exec.Cmd) {}

// runTray 非 Windows 平台无托盘
func runTray() {}
