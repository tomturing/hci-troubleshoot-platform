package main

// 交付并发度决策：既"稳"又"快"，且必须能按现网设备的真实能力兜底。
//
// 第一性原理
// ----------
// 并发度只由三个不可再分的量决定：**可用内存**（每片要占缓冲）、**CPU**（校验和与拷贝）、
// **出口带宽×分片大小**（并发才有收益）。任何一个不足，并发就是负收益：
// 内存不够会触发换页（Atom 机器 3.9GB 已经在 95%），带宽不足只会放大超时与重试。
//
// 因此策略是「保守起步 + 明确上限 + 允许强制覆盖」：
//   默认 1；内存 ≥4GB 且 ≥4 核 → 2；内存 ≥8GB 且 ≥8 核 → 4；
//   任何探测失败 → 1（宁可慢，不可抖）。
//
// 跨任务并发单独一档（默认 1）：两个 20GB 包同时传会互相拖到双双超时，
// 只有高配机才允许 2。

import (
	"runtime"
	"strings"
)

const (
	// 分片并发硬上限：再高在客户现场没有收益，只会放大重试风暴。
	maxDeliveryParts = 4
	// 跨任务并发硬上限：交付是"大块头"，同机两路已经是极限。
	maxDeliveryTasks = 2
)

// deliveryConcurrency 是决策结果，reason 用于写进事件日志（现场要能解释"为什么是 1"）。
type deliveryConcurrency struct {
	Parts  int
	Tasks  int
	Reason string
}

// decideDeliveryConcurrency 计算本次进程的交付并发度。
//
// 探测顺序：显式环境变量 → 硬件能力 → 保守默认。
func decideDeliveryConcurrency() deliveryConcurrency {
	cpu := runtime.NumCPU()
	memMB := availableMemoryMB()

	decision := deliveryConcurrency{Parts: 1, Tasks: 1, Reason: "low_resource"}
	if memMB >= 8*1024 && cpu >= 8 {
		decision = deliveryConcurrency{Parts: 4, Tasks: 2, Reason: "high_resource"}
	} else if memMB >= 4*1024 && cpu >= 4 {
		decision = deliveryConcurrency{Parts: 2, Tasks: 1, Reason: "medium_resource"}
	}

	// 内存约束：每片 4MB 缓冲 + 校验副本，按 128MB/片 保守折算。
	if limit := int(memMB / 128); limit < decision.Parts {
		decision.Parts = max(1, limit)
		decision.Reason = "memory_capped"
	}

	// 显式覆盖永远优先：现网特殊设备（大内存但弱 CPU、或需要强制单流）靠它兜底。
	if forced := envIntOrDefault("HCI_BRIDGE_DELIVERY_CONCURRENCY", 0); forced > 0 {
		decision.Parts, decision.Reason = clamp(forced, 1, maxDeliveryParts), "env:HCI_BRIDGE_DELIVERY_CONCURRENCY"
	}
	if forced := envIntOrDefault("HCI_BRIDGE_DELIVERY_TASKS", 0); forced > 0 {
		decision.Tasks = clamp(forced, 1, maxDeliveryTasks)
	}
	return decision
}

func clamp(value, low, high int) int {
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

// chunkConcurrencyFor 按分片大小再收一次：单片 64MB 时并发 4 会吃掉 256MB 缓冲，
// 在 4GB 机器上足以触发换页，因此按内存预算动态压低。
func chunkConcurrencyFor(decision deliveryConcurrency, chunkSize int64, memMB int64) int {
	if chunkSize <= 0 {
		return 1
	}
	// 只用可用内存的 1/8 做分片缓冲：剩余要留给页缓存、Go GC 与平台响应体，
	// 客户机本来就紧张，不能把预算算满。
	budgetMB := memMB / 8
	perPartMB := chunkSize / (1024 * 1024)
	if perPartMB <= 0 {
		perPartMB = 1
	}
	limit := int(budgetMB / perPartMB)
	if limit < 1 {
		limit = 1
	}
	return clamp(limit, 1, decision.Parts)
}

// describeDeliveryConcurrency 生成一行可写入日志的说明。
func describeDeliveryConcurrency(decision deliveryConcurrency) map[string]any {
	return map[string]any{
		"parts":  decision.Parts,
		"tasks":  decision.Tasks,
		"reason": decision.Reason,
		"cpu":    runtime.NumCPU(),
		"mem_mb": availableMemoryMB(),
	}
}

// normalizePlatformURL 去掉尾部斜杠，避免拼出 //api/... 被网关判成异常路径。
func normalizePlatformURL(raw string) string {
	return strings.TrimRight(strings.TrimSpace(raw), "/")
}
