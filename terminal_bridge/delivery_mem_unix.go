//go:build !windows

package main

// 非 Windows 的可用内存探测（cluster 模式跑在 Pod 里，主要给 K8s 与本地开发用）。
//
// 读 /proc/meminfo 的 MemAvailable：这是内核估算的"不触发换页还能分配多少"，
// 比 MemFree 更贴近真实可用量。读不到就返回 0，由调用方回落到最保守并发。

import (
	"bufio"
	"os"
	"strconv"
	"strings"
)

func availableMemoryMB() int64 {
	file, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "MemAvailable:") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			return 0
		}
		kb, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			return 0
		}
		return kb / 1024
	}
	return 0
}
