//go:build !windows

package main

// 非 Windows 的可用磁盘探测（交付前预检本地副本空间）。
//
// 为什么必须在拉取前算：12.8 GiB 的包要完整落到客户机本地再分片上传，
// 而拉取本身要 20 分钟以上。等写到一半才 ENOSPC，用户白等半小时、平台侧
// 分片一片都没有，且作业会以"拉取中断"这种与磁盘毫无关系的话术结束。

import "syscall"

// freeDiskBytes 返回 path 所在文件系统的可用字节数；探测失败返回 0（调用方跳过预检）。
func freeDiskBytes(path string) int64 {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0
	}
	return int64(stat.Bavail) * int64(stat.Bsize)
}
