//go:build windows

package main

// Windows 的可用磁盘探测（交付前预检本地副本空间）。
//
// 历史缺陷：这里长期 `return 0`，注释写着"宁可漏检，不可误拦"。
// 结果是所有 Windows 客户机都跳过了预检——88 MB 的包写到 9 MB 才 ENOSPC，
// 用户看到的是"拉取中断"，与磁盘毫无关系（2026-11-03 Q2026110300988）。
// 漏检的代价远大于误拦：误拦只需一次"仍然上传"，漏检要重新清盘 + 重拉。

import (
	"math"
	"strings"

	"golang.org/x/sys/windows"
)

// freeDiskBytes 返回 path 所在卷「当前进程可用」字节数；探测失败返回 0。
//
// 为什么用 GetDiskFreeSpaceEx 而不是 GetDiskFreeSpace：
//   - 前者返回 FreeBytesAvailableToCaller，已计入磁盘配额与卷保留；
//     后者只有卷总量，会把"有 100 GB 但配额只剩 1 MB"误判成充足。
//
// 为什么失败返回 0（而不是错误）：调用方把 0 当"未探测到"并放行预检。
// 客户机路径可能含中文、可能是 UNC、可能被安全软件拦住，
// 任何一处异常都不该变成"明明有空间却说不够"的新故障源。
func freeDiskBytes(path string) int64 {
	trimmed := strings.TrimSpace(path)
	if trimmed == "" {
		return 0
	}
	// GetDiskFreeSpaceExW 接受目录路径并自行解析卷；
	// UNC（\\server\share）必须以 \ 结尾，否则解析不到卷根。
	if strings.HasPrefix(trimmed, `\\`) && !strings.HasSuffix(trimmed, `\`) {
		trimmed += `\`
	}
	ptr, err := windows.UTF16PtrFromString(trimmed)
	if err != nil {
		return 0
	}
	var freeToCaller, totalBytes, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(ptr, &freeToCaller, &totalBytes, &totalFree); err != nil {
		return 0
	}
	// int64 溢出保护：极端大卷（>8 EiB）在现实中不存在，但截断成负数的后果
	// 是预检恒通过，必须显式封顶。
	if freeToCaller > uint64(math.MaxInt64) {
		return math.MaxInt64
	}
	return int64(freeToCaller)
}
