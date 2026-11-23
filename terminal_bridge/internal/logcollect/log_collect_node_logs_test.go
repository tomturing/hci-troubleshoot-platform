package logcollect

import (
	"strings"
	"testing"
)

// 采集范围必须覆盖 acli 的三类日志路径：根目录 acli.log、cmd_output、plugins/<name>。
// 只列一层会把 7 个插件与命令输出全部漏掉（Q2026112300134 现场就需要这些证据）。
func TestNodeLogListCommandCoversAllDepths(t *testing.T) {
	command := nodeLogListCommand()
	for _, glob := range []string{
		nodeLogRoot + "/*.log",
		nodeLogRoot + "/*/*.log",
		nodeLogRoot + "/*/*/*.log",
	} {
		if !strings.Contains(command, glob) {
			t.Fatalf("命令缺少 %s 层 glob：\n%s", glob, command)
		}
	}
	// 目录与不可读文件必须在节点侧先过滤，避免每个都白跑一次 tail。
	if !strings.Contains(command, `[ -f "$f" ]`) || !strings.Contains(command, `[ -r "$f" ]`) {
		t.Fatalf("命令必须过滤非文件与不可读路径：\n%s", command)
	}
}

func TestParseNodeLogPathsFiltersAndOrders(t *testing.T) {
	raw := strings.Join([]string{
		"/sf/log/today/acli/plugins/vm_suspend/vm_suspend.log",
		"/sf/log/today/acli/plugins/log_collect/collection_log.log",
		"/sf/log/today/acli/acli.log",
		"/sf/log/today/acli/cmd_output/acli_platform.node.list.log",
		"/sf/log/today/acli/plugins/log_collect/log_collect.log",
		"",                                  // 空行
		"/tmp/evil.log",                     // 越界
		"/sf/log/today/acli/../evil.log",    // 目录穿越
		"/sf/log/today/acli/a.txt",          // 非 .log
		"/sf/log/today/acli/x/y/z/deep.log", // 超过三层
		"/sf/log/today/acli/acli.log",       // 重复
		"/sf/log/today/acli_eval/other.log", // 前缀相似但不在根目录下
	}, "\n")

	paths := parseNodeLogPaths(raw)
	want := []string{
		"/sf/log/today/acli/acli.log",
		"/sf/log/today/acli/plugins/log_collect/collection_log.log",
		"/sf/log/today/acli/plugins/log_collect/log_collect.log",
		"/sf/log/today/acli/cmd_output/acli_platform.node.list.log",
		"/sf/log/today/acli/plugins/vm_suspend/vm_suspend.log",
	}
	if len(paths) != len(want) {
		t.Fatalf("过滤后条数 = %d, want %d: %v", len(paths), len(want), paths)
	}
	for index := range want {
		if paths[index] != want[index] {
			t.Fatalf("第 %d 条 = %q, want %q（顺序必须是 根日志 → log_collect → cmd_output → 其它插件）",
				index, paths[index], want[index])
		}
	}
}

// 无 .log 时返回空切片，调用方据此只留一条 INFO 事件，不当成失败。
func TestParseNodeLogPathsEmpty(t *testing.T) {
	if paths := parseNodeLogPaths("  \n\n"); len(paths) != 0 {
		t.Fatalf("空输出不应解析出路径：%v", paths)
	}
}

// 超限前必须先收高优先级文件：名额有限时 acli.log 与 log_collect 日志不能被别的插件挤掉。
func TestNodeLogPriorityRanking(t *testing.T) {
	cases := map[string]int{
		"/sf/log/today/acli/acli.log":                               0,
		"/sf/log/today/acli/plugins/log_collect/log_collect.log":    1,
		"/sf/log/today/acli/cmd_output/acli_platform.node.list.log": 2,
		"/sf/log/today/acli/plugins/vm_start/vm_start.log":          3,
		"/sf/log/today/acli/other/unknown.log":                      4,
	}
	for path, want := range cases {
		if got := nodeLogPriority(path); got != want {
			t.Fatalf("nodeLogPriority(%q) = %d, want %d", path, got, want)
		}
	}
}
