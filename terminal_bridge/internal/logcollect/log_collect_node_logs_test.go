package logcollect

import (
	"context"
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

// 三轮采集必须各自独立去重：init 轮的安装日志不能被终态轮吞掉。
func TestProcessLogDedupKeyPerPhase(t *testing.T) {
	const taskID = "task-phase-dedup"
	keys := map[string]bool{}
	for _, phase := range []string{
		processLogPhaseInit, processLogPhaseTerminal, processLogPhaseTerminalFail,
	} {
		key := processLogDedupKey(taskID, phase)
		if keys[key] {
			t.Fatalf("phase=%s 去重键与另一轮重复：%s", phase, key)
		}
		keys[key] = true
	}
	if processLogDedupKey(taskID, processLogPhaseInit) == processLogDedupKey("other-task", processLogPhaseInit) {
		t.Fatal("不同任务的去重键不应相同")
	}
}

// 进程日志必须覆盖所有目标主机，且发起节点始终在内（需求第 76 行）。
func TestResolveProcessLogHostsUsesExpectedSnapshot(t *testing.T) {
	cfg := EvidenceConfig{
		NodeIP:        "10.0.0.1",
		ExpectedHosts: []string{"10.0.0.2", "10.0.0.3", "bad-ip", "10.0.0.2"},
	}
	// nil 会话：只应返回 NodeIP + ExpectedHosts 中的合法项，不去探测 acli 节点清单。
	hosts := resolveProcessLogHosts(context.Background(), nil, cfg)
	want := []string{"10.0.0.1", "10.0.0.2", "10.0.0.3"}
	if len(hosts) != len(want) {
		t.Fatalf("主机数=%d want %d: %v", len(hosts), len(want), hosts)
	}
	for i := range want {
		if hosts[i] != want[i] {
			t.Fatalf("第 %d 台=%s want %s", i, hosts[i], want[i])
		}
	}
}

// 共享预算不能超发：多主机合计不得超过单任务上限。
func TestProcessLogBudgetShared(t *testing.T) {
	b := &processLogBudget{remaining: 100}
	if got := b.takeUpTo(60); got != 60 {
		t.Fatalf("首次额度=%d want 60", got)
	}
	if got := b.takeUpTo(60); got != 40 {
		t.Fatalf("第二次应被截断到剩余 40，实际 %d", got)
	}
	if got := b.takeUpTo(10); got != 0 {
		t.Fatalf("预算耗尽应为 0，实际 %d", got)
	}
}
