package logcollect

import (
	"sync"
)

// 逐台明细只在采集完成时上报一次：progress 轮询会重复同一个终态，
// 用任务 ID 去重避免把同一张表写进几百条桥日志；任务 ID 是 UUID，不会跨工单复用。
var logCollectHostResultsReported sync.Map

// logCollectHostResultExtra 把桥端已解析的逐台进度原样结构化，不推断状态、不补默认值。
// 字段取自 acli 进度原文的「IP 状态 百分比」行，错误列仅在 acli 提供时出现。
func logCollectHostResultExtra(hosts []logCollectHost) map[string]any {
	items := make([]map[string]any, 0, len(hosts))
	for _, host := range hosts {
		if host.IP == "" {
			continue
		}
		item := map[string]any{"ip": host.IP, "status": host.Status}
		if host.Percent != "" {
			item["percent"] = host.Percent
		}
		items = append(items, item)
	}
	return map[string]any{"hosts": items}
}

// markHostResultsReported 保证同一任务只落一条逐台证据；老任务 ID 不会因为进程重启而重复。
func markHostResultsReported(taskID string) bool {
	if taskID == "" {
		return false
	}
	_, loaded := logCollectHostResultsReported.LoadOrStore(taskID, true)
	return !loaded
}
