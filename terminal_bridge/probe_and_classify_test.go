package main

import "testing"

// TestClassifyExecFailure 覆盖修复 D：非零退出必须细分语义 error_type，
// 禁止把 jq 缺 ONIGURUMA / 命令缺失归为笼统错误（Q2026100812343 复盘）。
func TestClassifyExecFailure(t *testing.T) {
	cases := []struct {
		name     string
		exitCode int
		output   string
		want     string
	}{
		{"success_no_error", 0, "any output", ""},
		{"jq_oniguruma", 3, "jq: error (at <stdin>): ONIGURUMA regex library not available", "tool_capability_missing"},
		{"regex_not_compiled", 3, "regex could not be compiled", "tool_capability_missing"},
		{"command_not_found", 127, "bash: acli: command not found", "tool_capability_missing"},
		{"binary_missing", 127, "smartctl: not found", "tool_capability_missing"},
		{"no_such_file", 1, "ls: cannot access /dev/sdz: No such file or directory", "tool_capability_missing"},
		{"smartctl_no_such_device", 1, "Smartctl open device: /dev/sdb failed: No such device", "device_absent"},
		{"open_dev_enxio", 1, "cannot open /dev/sdb: No such device or address", "device_absent"},
		{"device_absent_zh", 1, "错误：目标设备不存在", "device_absent"},
		{"generic_nonzero", 2, "some unrelated failure", "nonzero_exit"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyExecFailure(tc.exitCode, tc.output); got != tc.want {
				t.Fatalf("classifyExecFailure(%d, %q) = %q, want %q", tc.exitCode, tc.output, got, tc.want)
			}
		})
	}
}

// TestProbeLevel 覆盖修复 E：探测成功/失败的日志级别判别。
func TestProbeLevel(t *testing.T) {
	if got := probeLevel(true); got != "INFO" {
		t.Fatalf("probeLevel(true) = %q, want INFO", got)
	}
	if got := probeLevel(false); got != "WARN" {
		t.Fatalf("probeLevel(false) = %q, want WARN", got)
	}
}

// TestExecResultErrorTypeField 锁定 ExecResult 结构化字段契约：超时/失败必须携带 ErrorType，
// 确保回传链路不塌缩为 unknown_error。
func TestExecResultErrorTypeField(t *testing.T) {
	res := ExecResult{Output: "x", ExitCode: 124, Timeout: true, ErrorType: "timeout"}
	if res.ErrorType != "timeout" {
		t.Fatalf("ExecResult.ErrorType 未透传: got %q", res.ErrorType)
	}
	if !res.Timeout {
		t.Fatal("ExecResult.Timeout 应为 true")
	}
}

// TestDeviceAbsentFromProbe 覆盖 §4/§5 device_absent 判别：
// 命令引用的 /dev 盘符与现场 lsblk 整盘清单交叉比对，准确区分“盘没了”与其他失败。
func TestDeviceAbsentFromProbe(t *testing.T) {
	cases := []struct {
		name       string
		command    string
		lsblk      string
		wantAbsent bool
		wantRefs   int
	}{
		{"disk_present", "smartctl -a /dev/sdb", "sda\nsdb\nsdc", false, 1},
		{"disk_absent", "smartctl -a /dev/sdb", "sda\nsdc", true, 1},
		{"partition_matches_disk", "smartctl -a /dev/sdb1", "sda sdb", false, 1},
		{"nvme_partition_matches", "acli --dev /dev/nvme0n1p2", "nvme0n1", false, 1},
		{"nvme_absent", "acli --dev /dev/nvme1n1", "nvme0n1", true, 1},
		{"no_device_ref", "lsblk -d", "sda sdb", false, 0},
		{"multi_all_absent", "compare /dev/sdb and /dev/sdd", "sda sdc", true, 2},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			absent, refs := deviceAbsentFromProbe(tc.command, tc.lsblk)
			if absent != tc.wantAbsent {
				t.Fatalf("deviceAbsentFromProbe absent = %v, want %v (refs=%v)", absent, tc.wantAbsent, refs)
			}
			if len(refs) != tc.wantRefs {
				t.Fatalf("referenced = %v (len %d), want len %d", refs, len(refs), tc.wantRefs)
			}
		})
	}
}
