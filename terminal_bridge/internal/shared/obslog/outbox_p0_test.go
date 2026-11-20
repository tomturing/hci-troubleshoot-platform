package obslog

// P0 回归守卫（2026-11-20 架构审查）：
//
//  1) 磁盘预算超限的 gap 上报不得在持锁路径回到 uploader —— 此前
//     enforceDiskBudgetLocked 里调 Emit，形成 persistLocked(持 u.Mu) → Emit →
//     publish → enqueue → u.Mu.Lock() 的同 goroutine 自死锁，桥整体挂死。
//  2) persistOK 必须是可复位的双向信号；故障期间 refill 必须暂停，否则
//     游标冻结 + 反复读回会造成无限重放。
//  3) 落盘失败且内存队列已满时必须计入 dropped 并留痕，不得静默丢弃。

import (
	"os"
	"strings"
	"testing"
	"time"
)

// resetGlobalUploaderForTest 把生产单例恢复到干净状态，返回还原函数。
// 这些测试刻意走全局 LogUpload（而非局部 newLogUploader）：死锁 bug 正是
// 因为既有测试全用局部实例、绕开了生产接线才逃逸的。
func resetGlobalUploaderForTest(t *testing.T, instance string) func() {
	t.Helper()
	u := LogUpload
	u.Mu.Lock()
	snapshot := struct {
		token, endpoint, instance string
		maxBytes, segmentBytes    int64
		queue                     []LogEntry
		outbox                    *os.File
		outboxAt                  string
		written                   int64
		dropped                   uint64
		spilled                   uint64
		persistOK                 bool
	}{
		token: u.token, endpoint: u.endpoint, instance: u.instance,
		maxBytes: u.maxBytes, segmentBytes: u.segmentBytes,
		queue: u.queue, outbox: u.outbox, outboxAt: u.outboxAt,
		written: u.written, dropped: u.dropped, spilled: u.spilled,
		persistOK: u.persistOK,
	}
	if u.outbox != nil {
		_ = u.outbox.Close()
	}
	u.outbox, u.outboxAt, u.written = nil, "", 0
	u.queue, u.seedQueue = nil, nil
	u.dropped, u.spilled = 0, 0
	u.persistOK = true
	u.instance = instance
	u.Mu.Unlock()
	return func() {
		u.Mu.Lock()
		defer u.Mu.Unlock()
		if u.outbox != nil {
			_ = u.outbox.Close()
		}
		u.token, u.endpoint, u.instance = snapshot.token, snapshot.endpoint, snapshot.instance
		u.maxBytes, u.segmentBytes = snapshot.maxBytes, snapshot.segmentBytes
		u.queue, u.outbox, u.outboxAt = snapshot.queue, snapshot.outbox, snapshot.outboxAt
		u.written, u.dropped, u.spilled = snapshot.written, snapshot.dropped, snapshot.spilled
		u.persistOK = snapshot.persistOK
	}
}

// TestGapReportDoesNotDeadlockOnDiskBudget 是 P0-1 的回归测试。
func TestGapReportDoesNotDeadlockOnDiskBudget(t *testing.T) {
	withLogDir(t, t.TempDir())
	restore := resetGlobalUploaderForTest(t, "p0-deadlock")
	defer restore()

	u := LogUpload
	u.Mu.Lock()
	u.token, u.endpoint = "tok", "http://127.0.0.1:1"
	u.maxBytes, u.segmentBytes = 1024, 512
	u.Mu.Unlock()

	done := make(chan struct{})
	go func() {
		defer close(done)
		big := strings.Repeat("x", 2000)
		for seq := 1; seq <= 200; seq++ {
			entry := seqEntries(seq, seq, "p0-deadlock")[0]
			entry.Message = big
			u.enqueue(entry)
		}
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("预算超限路径疑似自死锁：持锁调用会回到 enqueue 的路径（Emit→publish→enqueue→u.Mu）")
	}

	u.Mu.Lock()
	dropped := u.dropped
	u.Mu.Unlock()
	if dropped == 0 {
		t.Fatal("预算远低于写入量却无丢弃，本测试会空转，请检查 maxBytes/segmentBytes 设置")
	}
}

// TestPersistFailurePausesRefill 验证故障期间不重放、恢复后可继续消费。
func TestPersistFailurePausesRefill(t *testing.T) {
	withLogDir(t, t.TempDir())
	restore := resetGlobalUploaderForTest(t, "p0-refill")
	defer restore()

	u := LogUpload
	u.Mu.Lock()
	u.token, u.endpoint = "tok", "http://127.0.0.1:1"
	u.maxBytes, u.segmentBytes = 1<<20, 1<<20
	u.Mu.Unlock()

	for seq := 1; seq <= 3; seq++ {
		u.enqueue(seqEntries(seq, seq, "p0-refill")[0])
	}
	u.Mu.Lock()
	if len(u.queue) != 3 {
		u.Mu.Unlock()
		t.Fatalf("前置条件不成立：内存队列应有 3 条，实际 %d", len(u.queue))
	}
	u.queue = nil // 模拟已消费完，只留磁盘
	u.persistOK = false
	u.Mu.Unlock()

	u.Mu.Lock()
	u.refillFromDiskLocked()
	paused := len(u.queue)
	u.Mu.Unlock()
	if paused != 0 {
		t.Fatalf("persistOK=false 时不得从磁盘 refill（会无限重放），实际读回 %d 条", paused)
	}

	// 写成功一次即恢复：新增条目落盘后 persistOK 复位。
	u.enqueue(seqEntries(4, 4, "p0-refill")[0])
	u.Mu.Lock()
	if !u.persistOK {
		u.Mu.Unlock()
		t.Fatal("落盘成功后 persistOK 必须复位，否则游标永久冻结")
	}
	u.Mu.Unlock()

	u.Mu.Lock()
	u.queue = nil
	u.refillFromDiskLocked()
	recovered := len(u.queue)
	u.Mu.Unlock()
	if recovered == 0 {
		t.Fatal("恢复后应能从游标读回未消费数据")
	}
}

// TestWriteFailureCountsAsDroppedWhenQueueFull 验证磁盘满 + 队列满不再静默丢弃。
func TestWriteFailureCountsAsDroppedWhenQueueFull(t *testing.T) {
	withLogDir(t, t.TempDir())
	restore := resetGlobalUploaderForTest(t, "p0-lost")
	defer restore()

	u := LogUpload
	u.Mu.Lock()
	u.token, u.endpoint = "tok", "http://127.0.0.1:1"
	// 已关闭的文件句柄让 Write 必然失败，模拟磁盘满/句柄耗尽。
	closed, err := os.CreateTemp(t.TempDir(), "closed-*.log")
	if err != nil {
		u.Mu.Unlock()
		t.Fatalf("准备失败: %v", err)
	}
	_ = closed.Close()
	u.outbox, u.outboxAt = closed, "/nonexistent/seg-test.jsonl"
	u.written = 0
	// 内存队列填满，逼出"既进不了内存也落不了盘"的分支。
	u.queue = make([]LogEntry, memoryQueueCap)
	before := u.dropped
	u.Mu.Unlock()

	u.enqueue(seqEntries(1, 1, "p0-lost")[0])

	u.Mu.Lock()
	after := u.dropped
	persistOK := u.persistOK
	u.Mu.Unlock()
	if after != before+1 {
		t.Fatalf("落盘失败且队列满必须计入 dropped，实际 %d→%d", before, after)
	}
	if persistOK {
		t.Fatal("写失败后 persistOK 必须置 false")
	}
}
