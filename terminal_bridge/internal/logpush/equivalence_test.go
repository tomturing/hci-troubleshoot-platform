package logpush

// L1-L4 等价性验收（设计 §3.6）：构造令牌前历史、上限溢出、panic 模拟、进程重启四场景，
// 断言平台收到的 event_id 集合 ⊇ 本地保留窗口内条目集合（gap 标记区间除外）；
// gap 标记事件本身必须可查。另附磁盘预算缺口、水位安全、断点续扫、旧 outbox 迁移守卫。

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func gzipReader(raw []byte) (*gzip.Reader, error) {
	return gzip.NewReader(bytes.NewReader(raw))
}

// fakePlatform 模拟平台直传接收端：按 event_id 幂等去重，记录收到的全部条目。
type fakePlatform struct {
	mu       sync.Mutex
	received map[string]LogEntry
	order    []string
}

func newFakePlatform(t *testing.T) (*fakePlatform, *httptest.Server) {
	t.Helper()
	fp := &fakePlatform{received: make(map[string]LogEntry)}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if r.Header.Get("Content-Encoding") == "gzip" {
			reader, gzErr := gzipReader(raw)
			if gzErr != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			raw, _ = io.ReadAll(reader)
		}
		var body struct {
			Logs []LogEntry `json:"logs"`
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		fp.mu.Lock()
		for _, entry := range body.Logs {
			if _, dup := fp.received[entry.EventID]; !dup {
				fp.received[entry.EventID] = entry
				fp.order = append(fp.order, entry.EventID)
			}
		}
		fp.mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	return fp, server
}

func (fp *fakePlatform) has(eventID string) bool {
	fp.mu.Lock()
	defer fp.mu.Unlock()
	_, ok := fp.received[eventID]
	return ok
}

func (fp *fakePlatform) count() int {
	fp.mu.Lock()
	defer fp.mu.Unlock()
	return len(fp.received)
}

// drainUploader 手动串行排空（不启 worker，避免与手动 flush 竞态）。
func drainUploader(t *testing.T, u *logUploader) {
	t.Helper()
	for i := 0; i < 2000; i++ {
		sent, err := u.flushOnce()
		if err != nil {
			t.Fatalf("排空失败: %v", err)
		}
		if sent == 0 {
			return
		}
	}
	t.Fatal("排空超过 2000 批仍未完成")
}

func seqEntries(from, to int, instance string) []LogEntry {
	entries := make([]LogEntry, 0, to-from+1)
	for seq := from; seq <= to; seq++ {
		entries = append(entries, LogEntry{
			Type: "bridge_log", Seq: uint64(seq), Level: "INFO", Service: "terminal_bridge",
			Event: "exec.done", Message: fmt.Sprintf("事件 %d", seq),
			EventID: deterministicEventID(instance, uint64(seq)), BridgeInstanceID: instance,
		})
	}
	return entries
}

// 场景一：令牌前历史——多个历史文件（含轮转件 .log.1 与当前文件）里令牌启用前的条目，
// 配置令牌后必须全部到达平台，一条不漏（废除 2 文件/5000 条窗口）。
func TestEquivalencePreTokenHistory(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-pretoken"
	currentPath := filepath.Join(dir, "bridge-20261120-current.log")
	hubLogLocationMu.Lock()
	DefaultHub.Mu.Lock()
	DefaultHub.LogPath = currentPath
	DefaultHub.Mu.Unlock()
	hubLogLocationMu.Unlock()
	t.Cleanup(func() {
		hubLogLocationMu.Lock()
		DefaultHub.Mu.Lock()
		DefaultHub.LogPath = ""
		DefaultHub.Mu.Unlock()
		hubLogLocationMu.Unlock()
	})

	// 三个文件共 12 条：旧启动文件、轮转件（旧实现永远扫不到）、当前文件（旧实现 exclude）。
	writeLogFile(t, filepath.Join(dir, "bridge-20261101-aaaa1111.log"), seqEntries(1, 4, instance))
	writeLogFile(t, filepath.Join(dir, "bridge-20261110-bbbb2222.log.1"), seqEntries(5, 8, instance))
	writeLogFile(t, currentPath, seqEntries(9, 12, instance))

	fp, server := newFakePlatform(t)
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance

	if seeded := u.SeedFromLocalLogs(0, 0); seeded != 12 {
		t.Fatalf("应补传全部 12 条历史，实际 %d", seeded)
	}
	drainUploader(t, u)
	for seq := 1; seq <= 12; seq++ {
		if !fp.has(deterministicEventID(instance, uint64(seq))) {
			t.Fatalf("令牌前历史 seq=%d 未到达平台", seq)
		}
	}
	// 补传批次不得推进 seq 水位（否则崩溃后未发送的实时条目会被跳过）。
	if got := u.loadWatermark(); got != 0 {
		t.Fatalf("纯补传不得推进 seq 水位，实际 %d", got)
	}
}

// 场景二：上限溢出——实时事件超过内存队列上限，溢出部分落盘等待消费，
// 最终全部到达平台且 dropped=0（旧实现会静默丢弃超出 5000 的部分）。
func TestEquivalenceMemoryOverflowSpillsToDisk(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-overflow"

	fp, server := newFakePlatform(t)
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance

	total := memoryQueueCap + 1200
	for _, entry := range seqEntries(1, total, instance) {
		u.enqueue(entry)
	}
	u.Mu.Lock()
	spilled, dropped, queued := u.spilled, u.dropped, len(u.queue)
	u.Mu.Unlock()
	if spilled != 1200 || dropped != 0 {
		t.Fatalf("溢出应落盘而非丢弃：spilled=%d dropped=%d", spilled, dropped)
	}
	if queued != memoryQueueCap {
		t.Fatalf("内存队列应恰好满 %d，实际 %d", memoryQueueCap, queued)
	}
	drainUploader(t, u)
	if fp.count() != total {
		t.Fatalf("平台应收全 %d 条，实际 %d", total, fp.count())
	}
	for seq := 1; seq <= total; seq++ {
		if !fp.has(deterministicEventID(instance, uint64(seq))) {
			t.Fatalf("溢出条目 seq=%d 未到达平台", seq)
		}
	}
	if got := u.loadWatermark(); got != uint64(total) {
		t.Fatalf("实时水位应推进到 %d，实际 %d", total, got)
	}
}

// 场景三：panic 模拟——bridge-stderr.log 的非 JSON 行（panic 栈）在"重启"后
// 必须被包装为 bridge.stderr/ERROR 到达平台；字节水位确认送达后才推进。
func TestEquivalencePanicStderrReachesPlatform(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-panic"

	stderrPath := filepath.Join(dir, "bridge-stderr.log")
	content := "goroutine 1 [running]:\n" +
		"panic: runtime error: invalid memory address or nil pointer dereference\n" +
		"\tmain.doWork(0x0)\n" +
		"{\"level\":\"INFO\",\"msg\":\"误导入的结构化行，不应包装\"}\n"
	if err := os.WriteFile(stderrPath, []byte(content), 0o600); err != nil {
		t.Fatalf("写 stderr 失败: %v", err)
	}

	fp, server := newFakePlatform(t)
	// 模拟重启后的新 uploader：从持久化水位（首次为 0）续读。
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance
	if seeded := u.SeedFromLocalLogs(0, 0); seeded != 3 {
		t.Fatalf("应包装 3 行 panic 现场（JSON 行除外），实际 %d", seeded)
	}
	drainUploader(t, u)

	fp.mu.Lock()
	defer fp.mu.Unlock()
	stderrCount := 0
	for _, entry := range fp.received {
		if entry.Event != "bridge.stderr" {
			continue
		}
		stderrCount++
		if entry.Level != "ERROR" {
			t.Fatalf("stderr 包装条目必须是 ERROR，实际 %q", entry.Level)
		}
		if entry.StderrOffset <= 0 {
			t.Fatal("stderr 条目必须携带字节偏移")
		}
	}
	if stderrCount != 3 {
		t.Fatalf("平台应收到 3 条 bridge.stderr，实际 %d", stderrCount)
	}
	if !strings.Contains(content, "panic: runtime error") {
		t.Fatal("测试数据异常")
	}
	found := false
	for _, entry := range fp.received {
		if strings.Contains(entry.Message, "panic: runtime error") {
			found = true
		}
	}
	if !found {
		t.Fatal("panic 关键行必须原样可达平台")
	}
	// 字节水位推进到最后一条包装行的末尾（尾部 JSON 行不包装、不推水位）；
	// 重启后从水位续读，不得重复补传已确认区间。
	lastLineEnd := int64(strings.Index(content, "{"))
	if got := u.loadStderrWatermark(); got != lastLineEnd {
		t.Fatalf("stderr 字节水位应为 %d，实际 %d", lastLineEnd, got)
	}
	u2 := newLogUploader()
	u2.token, u2.endpoint, u2.instance = "tok", server.URL, instance
	if seeded := u2.SeedFromLocalLogs(0, 0); seeded != 0 {
		t.Fatalf("重启后已确认区间不得重复补传，实际 %d", seeded)
	}
}

// 场景四：进程重启——发送失败期间进程被杀，outbox 分段与游标仍在磁盘；
// 新进程恢复后未发送条目必须全部到达平台（旧实现读回清空，再挂即丢）。
func TestEquivalenceRestartRecoversUnsent(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-restart"

	failServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	u1 := newLogUploader()
	u1.token, u1.endpoint, u1.instance = "tok", failServer.URL, instance
	for _, entry := range seqEntries(1, 5, instance) {
		u1.enqueue(entry)
	}
	if _, err := u1.flushOnce(); err == nil {
		t.Fatal("503 应失败")
	}
	failServer.Close()
	// 进程"被杀"：u1 直接丢弃，不关闭 outbox 文件句柄以外的清理。

	fp, server := newFakePlatform(t)
	u2 := newLogUploader()
	u2.token, u2.endpoint, u2.instance = "tok", server.URL, instance
	u2.recoverOutbox()
	drainUploader(t, u2)
	for seq := 1; seq <= 5; seq++ {
		if !fp.has(deterministicEventID(instance, uint64(seq))) {
			t.Fatalf("重启后 seq=%d 未到达平台", seq)
		}
	}
	if got := u2.loadWatermark(); got != 5 {
		t.Fatalf("重启恢复后水位应推进到 5，实际 %d", got)
	}
}

// 磁盘预算缺口：总预算超限时 drop-oldest 段，平台必须能查到 log_upload.gap，
// 且全部 gap 区间的并集恰好覆盖未到达的条目（等价性的"gap 除外"口径）。
func TestDiskBudgetDropOldestEmitsGap(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-gap"

	fp, server := newFakePlatform(t)
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance
	// 小分段 + 小预算：旧段不断被滚出并丢弃，缺口逻辑可被确定性触发。
	u.segmentBytes = 4096
	u.maxBytes = 16384

	// 每条 ~2KB：100 条滚出多个分段，总磁盘远超 16KB 预算。
	big := strings.Repeat("x", 2000)
	for seq := 1; seq <= 100; seq++ {
		entry := seqEntries(seq, seq, instance)[0]
		entry.Message = big
		u.enqueue(entry)
	}
	drainUploader(t, u)

	fp.mu.Lock()
	defer fp.mu.Unlock()
	type gapRange struct{ min, max uint64 }
	var ranges []gapRange
	for _, entry := range fp.received {
		if entry.Event != "log_upload.gap" {
			continue
		}
		min := uint64(entry.Extra["seq_min"].(float64))
		max := uint64(entry.Extra["seq_max"].(float64))
		if min == 0 || max < min {
			t.Fatalf("gap 区间非法: [%d,%d]", min, max)
		}
		ranges = append(ranges, gapRange{min, max})
	}
	if len(ranges) == 0 {
		t.Fatal("磁盘预算超限必须上报 log_upload.gap，平台侧必须可查")
	}
	// 等价性：收到的集合 ∪ 全部 gap 区间 ⊇ 全部本地条目。
	missing := 0
	for seq := 1; seq <= 100; seq++ {
		if _, ok := fp.received[deterministicEventID(instance, uint64(seq))]; ok {
			continue
		}
		covered := false
		for _, r := range ranges {
			if uint64(seq) >= r.min && uint64(seq) <= r.max {
				covered = true
				break
			}
		}
		if !covered {
			t.Fatalf("seq=%d 既未到达平台也不在任何 gap 区间", seq)
		}
		missing++
	}
	if missing == 0 {
		t.Fatal("预算远低于总量，必须有真实丢弃发生")
	}
	if u.dropped == 0 {
		t.Fatal("drop-oldest 必须计入 dropped")
	}
}

// 水位安全：补传（低 seq）与实时（高 seq）交错时，补传批次不得把水位拉过
// 尚未发送的实时条目——否则崩溃后这些条目会被水位过滤永久丢失。
func TestSeedBatchDoesNotAdvanceSeqWatermark(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-wmsafe"
	writeLogFile(t, filepath.Join(dir, "bridge-20261101-aaaa1111.log"), seqEntries(1, 3, instance))

	fp, server := newFakePlatform(t)
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance
	u.SeedFromLocalLogs(0, 0)
	u.enqueue(seqEntries(10, 10, instance)[0])

	// 第一批是补传（seq 1-3）：水位必须不动。
	if sent, err := u.flushOnce(); err != nil || sent != 3 {
		t.Fatalf("首批应发 3 条补传，sent=%d err=%v", sent, err)
	}
	if got := u.loadWatermark(); got != 0 {
		t.Fatalf("补传批次不得推进水位，实际 %d", got)
	}
	// 第二批是实时（seq 10）：水位推进到 10。
	if sent, err := u.flushOnce(); err != nil || sent != 1 {
		t.Fatalf("次批应发 1 条实时，sent=%d err=%v", sent, err)
	}
	if got := u.loadWatermark(); got != 10 {
		t.Fatalf("实时批次后水位应为 10，实际 %d", got)
	}
	if fp.count() != 4 {
		t.Fatalf("平台应收 4 条，实际 %d", fp.count())
	}
}

// 断点续扫：单轮条数预算用尽时记录进度，下一轮从断点继续，最终一条不漏。
func TestSeedBudgetPersistsProgressAcrossRounds(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-budget"
	writeLogFile(t, filepath.Join(dir, "bridge-20261101-aaaa1111.log"), seqEntries(1, 10, instance))

	fp, server := newFakePlatform(t)
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance

	if seeded := u.SeedFromLocalLogs(0, 4); seeded != 4 {
		t.Fatalf("首轮预算 4 条，实际 %d", seeded)
	}
	if !u.loadSeedProgress().Pending {
		t.Fatal("预算用尽必须记录 pending 进度")
	}
	if seeded := u.SeedFromLocalLogs(0, 4); seeded != 4 {
		t.Fatalf("次轮应续补 4 条，实际 %d", seeded)
	}
	if seeded := u.SeedFromLocalLogs(0, 4); seeded != 2 {
		t.Fatalf("末轮应补剩余 2 条，实际 %d", seeded)
	}
	if u.loadSeedProgress().Pending {
		t.Fatal("扫完后 pending 必须清除")
	}
	drainUploader(t, u)
	for seq := 1; seq <= 10; seq++ {
		if !fp.has(deterministicEventID(instance, uint64(seq))) {
			t.Fatalf("续扫后 seq=%d 未到达平台", seq)
		}
	}
}

// 旧版单文件 outbox 迁移：升级前落盘的 <instance>.jsonl 必须被续传，不得丢失。
func TestLegacySingleFileOutboxMigrates(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)
	instance := "inst-legacy"
	outbox := outboxDir()
	if err := os.MkdirAll(outbox, 0o700); err != nil {
		t.Fatalf("建 outbox 目录失败: %v", err)
	}
	writeLogFile(t, filepath.Join(outbox, instance+".jsonl"), seqEntries(1, 3, instance))

	fp, server := newFakePlatform(t)
	u := newLogUploader()
	u.token, u.endpoint, u.instance = "tok", server.URL, instance
	u.recoverOutbox()
	drainUploader(t, u)
	for seq := 1; seq <= 3; seq++ {
		if !fp.has(deterministicEventID(instance, uint64(seq))) {
			t.Fatalf("旧 outbox 条目 seq=%d 未到达平台", seq)
		}
	}
	if _, err := os.Stat(filepath.Join(outbox, instance+".jsonl")); !os.IsNotExist(err) {
		t.Fatal("旧单文件应已迁为分段")
	}
}
