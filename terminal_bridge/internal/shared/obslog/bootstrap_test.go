package obslog

// 云端桥日志直传的单测。
//
// 守护三条不变量：
//  1. 部署期没注入端点/令牌时是彻底的空操作（桌面桥零影响）；
//  2. 注入后 enabled=source=env，且实例号取自 DefaultHub（服务端靠它绑实例）；
//  3. 进程重启只补"水位之后"的本地日志，已确认上传的区间不重复补。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func writeLogFile(t *testing.T, path string, entries []LogEntry) {
	t.Helper()
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatalf("创建日志文件失败: %v", err)
	}
	defer file.Close()
	for _, entry := range entries {
		line, err := json.Marshal(entry)
		if err != nil {
			t.Fatalf("序列化日志失败: %v", err)
		}
		if _, err := file.Write(append(line, '\n')); err != nil {
			t.Fatalf("写入日志失败: %v", err)
		}
	}
}

// withLogDir 临时把 DefaultHub 的落盘目录指向测试目录，退出时恢复。
// 写侧必须持锁：后台 seed/worker 经 hubLogLocation 快照读，publish 持 h.Mu 读。
//
// 退出时必须**先关掉指向测试目录的文件句柄**再恢复目录：
// DefaultHub 是进程级单例，logFile 一经打开就长期 append。若只改回 LogDir，
// 句柄仍指向测试目录，随后任何 publish/rotate 都会在 t.TempDir 的 RemoveAll
// 之后重建文件，报 "directory not empty"（CI 上已两次命中，本地因时序宽松难复现）。
func withLogDir(t *testing.T, dir string) {
	t.Helper()
	hubLogLocationMu.Lock()
	DefaultHub.Mu.Lock()
	oldDir := DefaultHub.LogDir
	oldPath := DefaultHub.LogPath
	oldSource := DefaultHub.LogDirSource
	DefaultHub.LogDir = dir
	DefaultHub.LogPath = ""
	DefaultHub.Mu.Unlock()
	hubLogLocationMu.Unlock()
	t.Cleanup(func() {
		hubLogLocationMu.Lock()
		DefaultHub.Mu.Lock()
		if DefaultHub.logFile != nil {
			_ = DefaultHub.logFile.Close()
			DefaultHub.logFile = nil
			DefaultHub.logFileBytes = 0
		}
		DefaultHub.LogDir = oldDir
		DefaultHub.LogPath = oldPath
		DefaultHub.LogDirSource = oldSource
		DefaultHub.Mu.Unlock()
		hubLogLocationMu.Unlock()
	})
}

func TestBootstrapFromEnvIsNoopWithoutConfig(t *testing.T) {
	t.Setenv(envLogUploadEndpoint, "")
	t.Setenv(envLogUploadToken, "")
	u := newLogUploader()
	if u.BootstrapFromEnv() {
		t.Fatal("未配置端点/令牌时不应启用直传")
	}
	if u.enabled() {
		t.Fatal("未配置时应保持 enabled=false")
	}
}

func TestBootstrapFromEnvEnablesUploader(t *testing.T) {
	t.Setenv(envLogUploadEndpoint, "http://127.0.0.1:1/api/bridge-logs/direct")
	t.Setenv(envLogUploadToken, "cluster-internal-token")
	u := newLogUploader()
	if !u.BootstrapFromEnv() {
		t.Fatal("注入端点与令牌后必须启用直传")
	}
	if !u.enabled() {
		t.Fatal("bootstrap 成功后 enabled 必须为 true")
	}
	u.Mu.Lock()
	source, instance := u.source, u.instance
	u.Mu.Unlock()
	if source != "env" {
		t.Fatalf("来源必须标记为 env，实际 %q", source)
	}
	if instance != DefaultHub.InstanceID {
		t.Fatalf("实例号必须取自 DefaultHub，实际 %q", instance)
	}
}

func TestSeedFromLocalLogsSkipsUploadedRange(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)

	entries := []LogEntry{
		{Seq: 1, EventID: "e1", Event: "bridge.startup", CaseID: "Q1"},
		{Seq: 2, EventID: "e2", Event: "ssh.connected", CaseID: "Q1"},
		{Seq: 3, EventID: "e3", Event: "exec.done", CaseID: "Q1"},
		{Seq: 4, EventID: "e4", Event: "exec.done", CaseID: "Q1"},
	}
	writeLogFile(t, filepath.Join(dir, "bridge-20260101-aaaa1111.log"), entries)

	u := newLogUploader()
	u.token = "t"
	u.endpoint = "http://127.0.0.1:1/api/bridge-logs/direct"
	u.instance = "inst-test"
	u.saveWatermark(2)

	seeded := u.SeedFromLocalLogs(1, 100)
	if seeded != 2 {
		t.Fatalf("只应补水位之后的 2 条，实际 %d", seeded)
	}
	u.Mu.Lock()
	queued := make([]uint64, 0, len(u.seedQueue))
	for _, entry := range u.seedQueue {
		queued = append(queued, entry.Seq)
	}
	u.Mu.Unlock()
	if len(queued) != 2 || queued[0] != 3 || queued[1] != 4 {
		t.Fatalf("补传队列应为 [3 4]，实际 %v", queued)
	}
}

func TestSeedFromLocalLogsIsNoopWhenDisabled(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	writeLogFile(t, filepath.Join(dir, "bridge-20260101-bbbb2222.log"), []LogEntry{
		{Seq: 1, EventID: "e1", Event: "bridge.startup"},
	})

	u := newLogUploader() // 未配置令牌
	if seeded := u.SeedFromLocalLogs(1, 100); seeded != 0 {
		t.Fatalf("未启用直传时不应补传，实际 %d 条", seeded)
	}
}

func TestWatermarkRoundTrip(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)

	u := newLogUploader()
	u.instance = "inst-watermark"
	if got := u.loadWatermark(); got != 0 {
		t.Fatalf("初始水位应为 0，实际 %d", got)
	}
	u.saveWatermark(42)
	if got := u.loadWatermark(); got != 42 {
		t.Fatalf("水位应为 42，实际 %d", got)
	}
	// 只增不减：更小的值不得把水位拉回去，否则会重复补传。
	u.saveWatermark(7)
	if got := u.loadWatermark(); got != 42 {
		t.Fatalf("水位必须单调递增，实际 %d", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "bridge-log-uploaded-inst-watermark.Seq")); err != nil {
		t.Fatalf("水位文件必须落在日志目录: %v", err)
	}
}
