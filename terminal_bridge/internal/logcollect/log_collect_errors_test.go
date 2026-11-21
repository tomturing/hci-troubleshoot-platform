package logcollect

// 业务侧失败归因测试。错误码契约本身的守卫已随契约迁到 internal/shared/logcontract。

import (
	"testing"

	"terminal_bridge/internal/shared/logcontract"
)

// TestLogCollectFailureLineSkipsWrapper 守住 2026-11-12 的关键回归：
// acli 真实原因在 FATA 包装行之前，不能再被跳过。
func TestLogCollectFailureLineSkipsWrapper(t *testing.T) {
	output := `2026/11/12 18:26:57 {"@timestamp":"2026-11-12T18:26:57","level":"error","content":"[0] internal/cpu_linux.go:29 open /sys/fs/cgroup/cpu/cpuset.cpus: no such file or directory"}
收集专项core不存在！
FATA[0000] execing command in container: command terminated with exit code 1
`
	if got := logCollectFailureLine(output); got != "收集专项core不存在！" {
		t.Fatalf("应取业务行，得到 %q", got)
	}
	if !logCollectModuleMissing(output) {
		t.Fatal("应识别出专项不存在")
	}
}

// TestLogCollectFailureLineKeepsTimestampedReason 原因行带时间戳前缀时，
// 只能剥前缀、不能整行丢弃（否则真实原因消失，界面又退回"日志收集失败"）。
func TestLogCollectFailureLineKeepsTimestampedReason(t *testing.T) {
	output := "2026/11/12 18:26:57 收集专项core不存在！\n"
	if got := logCollectFailureLine(output); got != "收集专项core不存在！" {
		t.Fatalf("应剥掉时间戳保留原因，得到 %q", got)
	}
	if !logCollectModuleMissing(output) {
		t.Fatal("带时间戳时也要识别出专项不存在")
	}
}

// TestLogCollectFailureLineEnglish 英文 locale 的原因行同样要能取到。
func TestLogCollectFailureLineEnglish(t *testing.T) {
	output := "collector started\nError: scenario core not found\n"
	if got := logCollectFailureLine(output); got != "Error: scenario core not found" {
		t.Fatalf("应取英文原因行，得到 %q", got)
	}
}

// TestLogCollectFailureLineFallback FATA 只能兜底，不能作为唯一来源。
func TestLogCollectFailureLineFallback(t *testing.T) {
	if got := logCollectFailureLine("FATA[0000] execing command in container: exit 1\n"); got == "" {
		t.Fatal("没有业务行时应保留 FATA 兜底")
	}
	if got := logCollectFailureLine(""); got != "" {
		t.Fatalf("空输出应返回空，得到 %q", got)
	}
}

// TestSpcErrorTitle 标题必须取自契约生成码表：测试不再手写第二份中文。
// 断言从 logcontract.Title 取期望值，文案改版时测试自动跟随唯一出处。
func TestSpcErrorTitle(t *testing.T) {
	cases := map[string]string{
		"host_auth_failed": logcontract.ErrSpcSSHAuthFailed,
		"host_timeout":     logcontract.ErrSpcSSHTimeout,
		"spc_dir_missing":  logcontract.ErrSpcDirMissing,
		"spc_file_missing": logcontract.ErrSpcFileMissing,
	}
	for errType, code := range cases {
		want := logcontract.Title(code)
		if want == "" {
			t.Fatalf("契约码 %s 缺少标题", code)
		}
		if got := SpcErrorTitle(errType); got != want {
			t.Fatalf("SpcErrorTitle(%s) = %q，期望契约标题 %q", errType, got, want)
		}
	}
	// 未知短名回退到聚合码标题：证明契约码表确有真实消费方，不是死代码。
	if got := SpcErrorTitle("unknown_short_name"); got == "" || got != logcontract.Title(logcontract.ErrSpcAggregate) {
		t.Fatalf("未知短名应回退到聚合码标题，得到 %q", got)
	}
}
