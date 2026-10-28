//go:build windows

package main

// Windows 的可用磁盘探测：返回 0 表示"未探测到"，调用方跳过预检而不是误判失败。
//
// 明确不做 GetDiskFreeSpaceEx 的 syscall 绑定：客户机以 Windows 为主，
// 一旦这里的实现写错（路径含中文、盘符大小写、相对路径），预检会变成新的
// "明明有空间却说不够"的故障源。宁可漏检，不可误拦。

func freeDiskBytes(path string) int64 {
	return 0
}
