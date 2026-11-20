package logcollect

import (
	"testing"
)

func TestLogCollectHostResultExtra(t *testing.T) {
	hosts := []logCollectHost{
		{IP: "10.0.0.1", Status: "成功", Percent: "100%"},
		{IP: "", Status: "脏数据"},
		{IP: "10.0.0.2", Status: "失败"},
	}
	extra := logCollectHostResultExtra(hosts)
	items, ok := extra["hosts"].([]map[string]any)
	if !ok || len(items) != 2 {
		t.Fatalf("逐台明细应过滤空 IP 并保留两台，实际 %#v", extra)
	}
	if items[0]["ip"] != "10.0.0.1" || items[0]["percent"] != "100%" {
		t.Fatalf("首台字段丢失：%#v", items[0])
	}
	if _, exists := items[1]["percent"]; exists {
		t.Fatalf("acli 未给百分比时不应伪造：%#v", items[1])
	}
}

func TestMarkHostResultsReportedOnce(t *testing.T) {
	taskID := "host-results-once-test"
	if !markHostResultsReported(taskID) {
		t.Fatal("首次应允许上报")
	}
	if markHostResultsReported(taskID) {
		t.Fatal("同一任务重复轮询不应再落一条")
	}
}
