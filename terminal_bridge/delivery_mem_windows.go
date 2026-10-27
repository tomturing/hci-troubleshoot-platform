//go:build windows

package main

// Windows 可用内存探测。
//
// 为什么不用 Go 运行时的内存统计：`runtime.MemStats` 反映的是"本进程从 OS 拿到多少"，
// 而交付并发必须看**整机还剩多少**——客户机 3.9GB 已用 95% 时，桥自己看起来还很"瘦"，
// 但再开 4 路 4MB 缓冲就会把系统推向换页。

import (
	"syscall"
	"unsafe"
)

// memoryStatusEx 对应 Win32 的 MEMORYSTATUSEX。
type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

// availableMemoryMB 返回可用物理内存（MB）；探测失败返回 0，由调用方回落到最保守并发。
func availableMemoryMB() int64 {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	globalMemoryStatusEx := kernel32.NewProc("GlobalMemoryStatusEx")

	status := memoryStatusEx{Length: uint32(unsafe.Sizeof(memoryStatusEx{}))}
	ret, _, _ := globalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&status)))
	if ret == 0 {
		return 0
	}
	return int64(status.AvailPhys / (1024 * 1024))
}
