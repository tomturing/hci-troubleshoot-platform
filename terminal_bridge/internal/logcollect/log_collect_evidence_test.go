package logcollect

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"terminal_bridge/internal/shared/models"
	"testing"
)

func TestConfigureEvidenceRequiresTicketAndPlatformURL(t *testing.T) {
	taskID := "task-configure-test"
	t.Cleanup(func() { EvidenceConfigs.Delete(taskID) })

	if _, ok := EvidenceFor(taskID); ok {
		t.Fatalf("未配置时不应存在证据上下文")
	}
	configureEvidence(models.InMessage{
		ExecID: taskID, EvidenceTicket: "t.1", PlatformURL: "not-a-url",
	}, "")
	if _, ok := EvidenceFor(taskID); ok {
		t.Fatalf("平台地址非法时不得启用证据上报")
	}
	configureEvidence(models.InMessage{
		ExecID: taskID, CaseID: "Q2026111200624", EvidenceTicket: "t.1",
		PlatformURL: "https://hci.example/", RemoteURL: "http://5888.example",
		TraceID: "tr-1", TransferTLSInsecure: true,
	}, "ui-1")
	cfg, ok := EvidenceFor(taskID)
	if !ok {
		t.Fatalf("合法配置应启用证据上报")
	}
	if cfg.PlatformURL != "https://hci.example" {
		t.Fatalf("平台地址应去掉尾部斜杠，实际 %q", cfg.PlatformURL)
	}
	if cfg.UploadMode != "acli_direct" {
		t.Fatalf("带 remote_url 时应判 P1，实际 %q", cfg.UploadMode)
	}
	if !cfg.TLSInsecure || cfg.CustomUI != "ui-1" {
		t.Fatalf("TLS 授权与 custom_ui 必须传递，实际 %+v", cfg)
	}
}

func TestPostEvidenceCarriesTicketAndTrace(t *testing.T) {
	var seen sync.Map
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.Store("path", r.URL.Path)
		seen.Store("ticket", r.Header.Get("X-Evidence-Ticket"))
		seen.Store("trace", r.Header.Get("X-Trace-ID"))
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		seen.Store("body", body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	cfg := EvidenceConfig{
		TaskID: "task-http", PlatformURL: server.URL, Ticket: "t.2", TraceID: "tr-2",
	}
	ack, err := cfg.PostEvidence(context.Background(), cfg.TaskPath("metrics"), TransferMetric{
		Seq: 7, Stage: "uploading", BytesDone: 1024, Source: "bridge",
	}, "")
	if err != nil {
		t.Fatalf("上报失败: %v", err)
	}
	if ack["ok"] != true {
		t.Fatalf("响应解析失败: %v", ack)
	}
	if value, _ := seen.Load("ticket"); value != "t.2" {
		t.Fatalf("证据票据缺失: %v", value)
	}
	if value, _ := seen.Load("trace"); value != "tr-2" {
		t.Fatalf("调用链缺失: %v", value)
	}
	if value, _ := seen.Load("path"); value != "/api/direct/log-collect/tasks/task-http/metrics" {
		t.Fatalf("路径错误: %v", value)
	}
	body, _ := seen.Load("body")
	facts := body.(map[string]any)
	if facts["stage"] != "uploading" || facts["seq"] != float64(7) {
		t.Fatalf("事实上报内容错误: %v", facts)
	}
}

func TestInspectLocalArchiveReadsCentralDirectoryOnly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "archive.zip")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := zip.NewWriter(file)
	for _, name := range []string{"logs/a.log", "logs/b.log", "meta.json"} {
		entry, createErr := writer.Create(name)
		if createErr != nil {
			t.Fatal(createErr)
		}
		_, _ = entry.Write([]byte("0123456789"))
	}
	_ = writer.Close()
	_ = file.Close()

	manifest := inspectLocalArchive(path)
	if !manifest.Readable || manifest.EntryCount != 3 || manifest.EntryTotalBytes != 30 {
		t.Fatalf("中央目录读取错误: %+v", manifest)
	}
	if manifest.ArchiveSize <= 0 || manifest.HasEncrypted {
		t.Fatalf("包体积/加密标记错误: %+v", manifest)
	}
	if len(manifest.Entries) != 3 || manifest.Entries[0].Name != "a.log" {
		t.Fatalf("条目清单错误: %+v", manifest.Entries)
	}
}

func TestInspectLocalArchiveUnreadableIsNotClaimedAsValid(t *testing.T) {
	path := filepath.Join(t.TempDir(), "broken.zip")
	if err := os.WriteFile(path, []byte("not a zip"), 0o600); err != nil {
		t.Fatal(err)
	}
	manifest := inspectLocalArchive(path)
	if manifest.Readable || manifest.EntryCount != 0 {
		t.Fatalf("读不出来的包不能标成可读: %+v", manifest)
	}
	if manifest.Evidence["reason"] == "" {
		t.Fatalf("失败原因必须留证")
	}
}

func TestMeasureNetworkUnreachableReportsLossNotZero(t *testing.T) {
	sample := measureNetwork(context.Background(), "http://127.0.0.1:1", false)
	if sample.Reachable || sample.LossRate != 1 || sample.SampleCount != evidenceProbeSamples {
		t.Fatalf("不可达目标应报告 100%% 丢包而不是 0: %+v", sample)
	}
	if sample.RTTAvgMS != nil {
		t.Fatalf("没有成功样本时不应伪造 RTT")
	}
}

func TestBandwidthAdvice(t *testing.T) {
	if got := bandwidthAdvice(0, 0, false); got == "" {
		t.Fatal("不可达时必须给客户提示")
	}
	if got := bandwidthAdvice(1024, 10*1024*1024, true); got == "" {
		t.Fatal("上行过低时必须提示换时段或换路径")
	}
	if got := bandwidthAdvice(10*1024*1024, 10*1024*1024, true); got != "" {
		t.Fatalf("链路良好时不应制造误导性告警: %s", got)
	}
}

// buildZipFixture 生成真实 zip 字节：节点侧解析必须能处理真实中央目录，
// 用字符串桩会把「读不出条目」测成绿灯。
func buildZipFixture(t *testing.T, entries map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	writer := zip.NewWriter(&buf)
	names := make([]string, 0, len(entries))
	for name := range entries {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		entry, err := writer.Create(name)
		if err != nil {
			t.Fatalf("构造 zip 条目失败: %v", err)
		}
		if _, err := entry.Write([]byte(entries[name])); err != nil {
			t.Fatalf("写入 zip 条目失败: %v", err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("关闭 zip 失败: %v", err)
	}
	return buf.Bytes()
}

// markZipCentralDirectoryEncrypted 把中央目录里的加密标志位打开。
// 客户包全条目加密是常态，这条路径必须可读——这正是旧实现（zipinfo -t）误判的地方。
func markZipCentralDirectoryEncrypted(data []byte) []byte {
	out := append([]byte(nil), data...)
	for i := 0; i+10 < len(out); i++ {
		if out[i] == 0x50 && out[i+1] == 0x4b && out[i+2] == 0x01 && out[i+3] == 0x02 {
			out[i+8] |= 0x1
		}
	}
	return out
}

func TestFillManifestFromZipTailReadsCentralDirectory(t *testing.T) {
	data := buildZipFixture(t, map[string]string{"a.log": "hello", "b.log": "world!!"})
	manifest, err := fillManifestFromZipTail(data, int64(len(data)), "/sf/x/Q.zip")
	if err != nil {
		t.Fatalf("真实 zip 必须可解析: %v", err)
	}
	if !manifest.Readable {
		t.Fatalf("可解析时 readable 必须为真")
	}
	if manifest.EntryCount != 2 {
		t.Fatalf("条目数错误: %d", manifest.EntryCount)
	}
	if manifest.EntryTotalBytes != int64(len("hello")+len("world!!")) {
		t.Fatalf("原始总字节错误: %d", manifest.EntryTotalBytes)
	}
	if manifest.HasEncrypted {
		t.Fatalf("未加密包不得标记加密")
	}
	if manifest.Source != "node_zip" || manifest.ArchivePath != "/sf/x/Q.zip" {
		t.Fatalf("来源/路径必须如实回传: %+v", manifest)
	}
}

func TestFillManifestFromZipTailKeepsEncryptedArchiveReadable(t *testing.T) {
	data := markZipCentralDirectoryEncrypted(buildZipFixture(t, map[string]string{"a.log": "hello"}))
	manifest, err := fillManifestFromZipTail(data, int64(len(data)), "/sf/x/Q.zip")
	if err != nil || !manifest.Readable {
		t.Fatalf("加密包中央目录仍可读，不得判不可读: err=%v manifest=%+v", err, manifest)
	}
	if !manifest.HasEncrypted {
		t.Fatalf("加密标志必须如实上报：S7 判据依赖它区分内容质量")
	}
}

func TestFillManifestFromZipTailRejectsTooSmallWindow(t *testing.T) {
	data := buildZipFixture(t, map[string]string{"a.log": "hello"})
	tail := data[len(data)-10:]
	if _, err := fillManifestFromZipTail(tail, int64(len(data)), "/sf/x/Q.zip"); err == nil {
		t.Fatalf("窗口不足必须报错，交给上层放大窗口重试")
	}
}

func TestArchiveTailWindows(t *testing.T) {
	if got := archiveTailWindows(1024); len(got) != 1 || got[0] != 1024 {
		t.Fatalf("小包只取自身大小: %v", got)
	}
	got := archiveTailWindows(64 << 20)
	if len(got) != 2 || got[0] != archiveTailProbeBytes || got[1] != archiveTailMaxBytes {
		t.Fatalf("大包应由小到大两档: %v", got)
	}
}

func TestArchiveHealthVerdict(t *testing.T) {
	// 与平台 derive_manifest_quality 同口径：桥侧只给结论，平台给码。
	cases := []struct {
		name       string
		size       int64
		entryCount int
		entryBytes int64
		readable   bool
		want       string
	}{
		{"不可读", 8 << 20, 0, 0, false, "unreadable"},
		{"体积未知", 0, 0, 0, true, "missing"},
		{"偏小", 1024, 1, 1024, true, "too_small"},
		{"空包", 4 << 20, 0, 0, true, "empty_archive"},
		{"仅元数据", 4 << 20, 2, 2048, true, "metadata_only"},
		{"内容偏少", 4 << 20, 10, 2048, true, "content_too_small"},
		{"正常", 8 << 20, 10, 8 << 20, true, "ok"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := archiveHealthVerdict(tc.size, tc.entryCount, tc.entryBytes, tc.readable); got != tc.want {
				t.Fatalf("verdict=%s want %s", got, tc.want)
			}
		})
	}
}
