package main

// 磁盘准入与磁盘满归类的单测。
//
// 这些用例钉住的都是现场真实发生过的错配：
//   - 88 MB 的包只写进 9 MB 才报错（预检缺失）；
//   - 报错被包装成"拉取中断"（磁盘满未分类）；
//   - 5 次秒级重试全打在同一处 ENOSPC（重试策略未区分）。

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

// withDiskFree 替换磁盘探测实现。probed=false 模拟探测不可用（Windows 上
// 权限/UNC/安全软件拦截时 freeDiskBytes 返回 0 的语义）。
func withDiskFree(t *testing.T, free int64, probed bool) {
	t.Helper()
	original := diskFreeFunc
	t.Cleanup(func() { diskFreeFunc = original })
	diskFreeFunc = func(string) int64 {
		if !probed {
			return 0
		}
		return free
	}
}

func TestDiskNeedAddsSlack(t *testing.T) {
	// 小包：固定余量生效（88 MB 的包至少要多留 64 MiB）
	small := int64(1 << 20)
	if got, want := diskNeed(small), small+deliveryDiskSlackMin; got != want {
		t.Fatalf("小包余量错误: got=%d want=%d", got, want)
	}
	// 大包：5% 余量生效（64 MiB 相对 10 GiB 太小）
	big := int64(10) << 30
	if got, want := diskNeed(big), big+big/deliveryDiskSlackDiv; got != want {
		t.Fatalf("大包余量错误: got=%d want=%d", got, want)
	}
	// 无需拉取时不要求空间
	if got := diskNeed(0); got != 0 {
		t.Fatalf("零需求应返回 0，实际 %d", got)
	}
}

func TestPrecheckDiskRejectsWhenFreeBelowNeed(t *testing.T) {
	withDiskFree(t, 9<<20, true) // 现场实测：只剩 8.9 MiB
	local := filepath.Join(t.TempDir(), "task.zip")
	check := precheckDisk(local, 92325257, 0)
	if check.OK {
		t.Fatal("可用空间远小于需求时必须拒绝")
	}
	if !check.Probed {
		t.Fatal("探测成功时 Probed 必须为 true")
	}
	if check.Free != 9<<20 {
		t.Fatalf("可用字节未回传: %d", check.Free)
	}
	if check.Need != diskNeed(92325257) {
		t.Fatalf("需求字节未回传: %d", check.Need)
	}
}

func TestPrecheckDiskPassesWhenProbeUnavailable(t *testing.T) {
	withDiskFree(t, 0, false)
	check := precheckDisk(filepath.Join(t.TempDir(), "task.zip"), 92325257, 0)
	if !check.OK {
		t.Fatal("探测不可用必须放行：未知 ≠ 不足")
	}
	if check.Probed {
		t.Fatal("探测失败时 Probed 必须为 false")
	}
}

func TestPrecheckDiskAccountsForExistingBytes(t *testing.T) {
	const total = int64(100 << 20)
	// 已拉 90 MiB：只剩 10 MiB + 64 MiB 余量 = 74 MiB
	withDiskFree(t, 80<<20, true)
	if check := precheckDisk(filepath.Join(t.TempDir(), "a.zip"), total, 90<<20); !check.OK {
		t.Fatalf("80 MiB 可用应满足 74 MiB 需求: %+v", check)
	}
	withDiskFree(t, 70<<20, true)
	if check := precheckDisk(filepath.Join(t.TempDir(), "a.zip"), total, 90<<20); check.OK {
		t.Fatalf("70 MiB 可用不满足 74 MiB 需求，应拒绝: %+v", check)
	}
}

func TestPrecheckDiskSkipsWhenAlreadyComplete(t *testing.T) {
	withDiskFree(t, 0, true)
	check := precheckDisk(filepath.Join(t.TempDir(), "a.zip"), 100, 100)
	if !check.OK || check.Need != 0 {
		t.Fatalf("本地已完整时不应要求空间: %+v", check)
	}
}

func TestIsLocalDiskFull(t *testing.T) {
	pathErr := &os.PathError{Op: "write", Path: `C:\Users\luo\AppData\Local\Temp\hci-log-collect\x.zip`, Err: syscall.Errno(112)}
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"windows-disk-full", pathErr, true},
		{"windows-handle-disk-full", syscall.Errno(39), true},
		{"unix-enospc", syscall.Errno(28), true},
		{"english-message", errors.New("write x.zip: There is not enough space on the disk."), true},
		{"unix-message", errors.New("write x.zip: no space left on device"), true},
		{"network", errors.New("connection reset by peer"), false},
		{"wrapped-sentinel", errors.Join(errLocalDiskFull), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isLocalDiskFull(tc.err); got != tc.want {
				t.Fatalf("isLocalDiskFull(%v) = %v, want %v", tc.err, got, tc.want)
			}
		})
	}
}

func TestDeliveryDiskFullErrorCarriesFacts(t *testing.T) {
	inner := &os.PathError{Op: "write", Path: `C:\Temp\x.zip`, Err: syscall.Errno(112)}
	failure := &deliveryDiskFullError{
		Written: 9355264, Need: diskNeed(92325257 - 9355264), Free: 9 << 20,
		LocalPath: `C:\Users\luo\AppData\Local\Temp\hci-log-collect\c4b64715.zip`, Cause: inner,
	}

	if !isDeliveryDiskFull(failure) {
		t.Fatal("isDeliveryDiskFull 应识别自身类型")
	}
	message := failure.Error()
	for _, fragment := range []string{"客户机磁盘空间不足", "8.9 MiB", "已写入", "无需重新采集"} {
		if !strings.Contains(message, fragment) {
			t.Fatalf("文案缺少 %q: %s", fragment, message)
		}
	}
	// 原始 errno 必须可穿透：上层仍要能判断"这是磁盘满"而不是网络问题。
	if !isLocalDiskFull(errors.Unwrap(failure)) {
		t.Fatal("Unwrap 后应能识别原始 ENOSPC")
	}
	if got := deliveryErrorCode(failure); got != ErrTransferLocalDiskFull {
		t.Fatalf("磁盘满必须映射成 %s，实际 %q", ErrTransferLocalDiskFull, got)
	}
	if got := deliveryErrorCode(errors.New("connection reset")); got != "" {
		t.Fatalf("非磁盘错误不应编造错误码，实际 %q", got)
	}
}

func TestProgressStepAdaptsToTotal(t *testing.T) {
	// 88 MB 的包：1% ≈ 883 KiB < 4 MiB 下限 → 取 4 MiB，约 22 次上报。
	// 旧实现固定 64 MiB，全程一次都不上报，pulled_bytes 恒为 0。
	if got, want := progressStep(92325257), int64(4<<20); got != want {
		t.Fatalf("小包步进错误: got=%d want=%d", got, want)
	}
	// 20 GiB 的包：1% > 64 MiB 上限 → 取 64 MiB，避免上万次消息。
	if got, want := progressStep(20<<30), int64(64<<20); got != want {
		t.Fatalf("大包步进错误: got=%d want=%d", got, want)
	}
	// 总量未知时按上限走（保守，减少无意义的消息）。
	if got := progressStep(0); got != 64<<20 {
		t.Fatalf("未知总量应取上限，实际 %d", got)
	}
}

// ── 并发准入（2026-11-03 补齐）─────────────────────────────────────────────
//
// 现场形态：一台客户机上同时有多个交付作业，各自预检都"够"，
// 于是都放行、一起把 %TEMP%/hci-log-collect 写满。单任务预检看不到
// "别人还要占多少"，必须把同盘进行中作业的未落地字节叠加进来。

func TestAdmissionCheckStacksReservedBytes(t *testing.T) {
	const total = int64(100 << 20)
	local := filepath.Join(t.TempDir(), "task.zip")
	// 单任务：剩余 100 MiB + 64 MiB 余量 = 164 MiB；可用 200 MiB，本来能过。
	withDiskFree(t, 200<<20, true)
	if v := admissionCheck(local, total, 0, 0); !v.OK || v.Concurrent {
		t.Fatalf("无并发时应通过: %+v", v)
	}
	// 另一个作业还要占 60 MiB：需要 224 MiB > 200 MiB，必须拒绝且归因为并发。
	v := admissionCheck(local, total, 0, 60<<20)
	if v.OK {
		t.Fatal("叠加占用后空间不足，必须拒绝")
	}
	if !v.Concurrent {
		t.Fatal("单任务本来能过、只因叠加而失败，必须标记 Concurrent")
	}
	if want := diskNeed(total) + 60<<20; v.Need != want {
		t.Fatalf("需求应包含预留: got=%d want=%d", v.Need, want)
	}
	if v.Reserved != 60<<20 {
		t.Fatalf("预留字节未回传: %d", v.Reserved)
	}
}

func TestAdmissionCheckKeepsSingleJobVerdictWhenAlreadyShort(t *testing.T) {
	// 单任务本身就不够（现场只剩 8.9 MiB）：即使有并发占用，
	// 也不能说成"并发导致"——否则用户被告知去等，等完依然失败。
	withDiskFree(t, 9<<20, true)
	check := admissionCheck(filepath.Join(t.TempDir(), "t.zip"), 92325257, 0, 1<<20)
	if check.OK {
		t.Fatal("空间不足必须拒绝")
	}
	if check.Concurrent {
		t.Fatal("单任务本身就不够时不能归因为并发")
	}
}

func TestAdmissionCheckPassesWhenProbeUnavailable(t *testing.T) {
	withDiskFree(t, 0, false)
	v := admissionCheck(filepath.Join(t.TempDir(), "t.zip"), 92325257, 0, 10<<30)
	if !v.OK || v.Concurrent {
		t.Fatalf("探测不可用必须放行，且不得归因为并发: %+v", v)
	}
}

func TestDeliveryAdmissionErrorMapsToCode(t *testing.T) {
	failure := &deliveryAdmissionError{
		Need: 100 << 20, Free: 90 << 20, Reserved: 50 << 20,
		LocalPath: `C:\Users\luo\AppData\Local\Temp\hci-log-collect\x.zip`,
	}
	if !isDeliveryAdmission(failure) {
		t.Fatal("isDeliveryAdmission 应识别自身类型")
	}
	if got := deliveryErrorCode(failure); got != ErrTransferAdmissionExceeded {
		t.Fatalf("并发准入必须映射成 %s，实际 %q", ErrTransferAdmissionExceeded, got)
	}
	message := failure.Error()
	for _, fragment := range []string{"并发", "等待其它任务", "无需重新采集", "50.0 MiB"} {
		if !strings.Contains(message, fragment) {
			t.Fatalf("文案缺少 %q: %s", fragment, message)
		}
	}
}

func TestPendingLocalBytesCountsOnlyActiveSameDir(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "hci-log-collect")
	elsewhere := filepath.Join(t.TempDir(), "other")
	r := &deliveryRunner{jobs: map[string]*deliveryJob{
		// 自己不算
		"self": {JobID: "self", Phase: phasePulling, SourceSize: 100, PulledBytes: 10, LocalPath: filepath.Join(dir, "self.zip")},
		// 同目录进行中：80-20=60
		"other": {JobID: "other", Phase: phasePulling, SourceSize: 80, PulledBytes: 20, LocalPath: filepath.Join(dir, "other.zip")},
		// 终态不算
		"done": {JobID: "done", Phase: phaseDone, SourceSize: 70, LocalPath: filepath.Join(dir, "done.zip")},
		// 大小未知不估算
		"unknown": {JobID: "unknown", Phase: phasePulling, SourceSize: 0, LocalPath: filepath.Join(dir, "u.zip")},
		// 不同目录不算
		"elsewhere": {JobID: "elsewhere", Phase: phasePulling, SourceSize: 60, LocalPath: filepath.Join(elsewhere, "x.zip")},
	}}
	if got, want := r.pendingLocalBytes("self", dir), int64(60); got != want {
		t.Fatalf("只应统计同目录进行中作业的未落地字节: got=%d want=%d", got, want)
	}
	// 已有部分落地时按剩余算，不按总量算（否则会把已写的字节重复预留一次）。
	r.jobs["other"].PulledBytes = 80
	if got := r.pendingLocalBytes("self", dir); got != 0 {
		t.Fatalf("已落地完的作业不应再预留: got=%d", got)
	}
}
