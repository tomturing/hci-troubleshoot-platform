package obslog

// 补传队列合并的回归：无 fsync、无 O(n²) 之后，队列顺序语义必须一字不差。
//
// 为什么重点测顺序：seedQueue 的顺序是"低 seq 先发"的唯一保证，水位只在
// 实时批次成功后推进；顺序一旦错乱，崩溃后要么重复补传、要么跳过未发条目。

import (
	"fmt"
	"path/filepath"
	"testing"
)

func seqsOf(entries []LogEntry) []uint64 {
	out := make([]uint64, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Seq)
	}
	return out
}

func assertSeqs(t *testing.T, got []LogEntry, want []uint64) {
	t.Helper()
	seqs := seqsOf(got)
	if len(seqs) != len(want) {
		t.Fatalf("队列长度 %d，期望 %d（实际 %v）", len(seqs), len(want), seqs)
	}
	for i := range want {
		if seqs[i] != want[i] {
			t.Fatalf("队列顺序漂移：实际 %v，期望 %v", seqs, want)
		}
	}
}

// 多批合并后仍保持全局升序（模拟多轮续扫 + 实时流量交错）。
func TestMergeSeedBatchKeepsOrderAcrossBatches(t *testing.T) {
	u := newLogUploader()
	u.mergeSeedBatchLocked([]LogEntry{{Seq: 3}, {Seq: 7}})
	u.mergeSeedBatchLocked([]LogEntry{{Seq: 4}, {Seq: 5}, {Seq: 9}})
	u.mergeSeedBatchLocked([]LogEntry{{Seq: 11}})
	u.Mu.Lock()
	defer u.Mu.Unlock()
	assertSeqs(t, u.seedQueue, []uint64{3, 4, 5, 7, 9, 11})
}

// stderr 包装条目（Seq=0）没有 seq 语义，必须恒排尾部，且彼此相对顺序不变。
func TestMergeSeedBatchKeepsZeroSeqAtTail(t *testing.T) {
	u := newLogUploader()
	u.mergeSeedBatchLocked([]LogEntry{
		{Seq: 1},
		{Seq: 0, Event: "bridge.stderr", StderrOffset: 10},
		{Seq: 0, Event: "bridge.stderr", StderrOffset: 20},
	})
	u.mergeSeedBatchLocked([]LogEntry{{Seq: 2}})
	u.Mu.Lock()
	defer u.Mu.Unlock()
	assertSeqs(t, u.seedQueue, []uint64{1, 2, 0, 0})
	if u.seedQueue[2].StderrOffset != 10 || u.seedQueue[3].StderrOffset != 20 {
		t.Fatalf("同序条目的相对顺序被改变: %d, %d", u.seedQueue[2].StderrOffset, u.seedQueue[3].StderrOffset)
	}
}

// 超过一个合并批次（seedMergeBatchSize）的扫描：defer 兜底与中途 flush 都要生效，
// 且不能漏条目——这正是原实现 O(n²) 在 5000 条预算下会拖慢的路径。
func TestSeedScanBeyondOneMergeBatch(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)

	const total = seedMergeBatchSize + 88
	entries := make([]LogEntry, 0, total)
	for i := 1; i <= total; i++ {
		entries = append(entries, LogEntry{
			Seq:     uint64(i),
			EventID: fmt.Sprintf("e%d", i),
			Event:   "exec.done",
		})
	}
	writeLogFile(t, filepath.Join(dir, "bridge-20260102-cccc3333.log"), entries)

	u := newLogUploader()
	u.token = "t"
	u.endpoint = "http://127.0.0.1:1/api/bridge-logs/direct"
	u.instance = "inst-merge"
	if seeded := u.SeedFromLocalLogs(1, total); seeded != total {
		t.Fatalf("应补传 %d 条，实际 %d", total, seeded)
	}

	u.Mu.Lock()
	queued := append([]LogEntry(nil), u.seedQueue...)
	u.Mu.Unlock()
	if len(queued) != total {
		t.Fatalf("入队 %d 条，期望 %d", len(queued), total)
	}
	for i := 1; i < len(queued); i++ {
		if queued[i].Seq <= queued[i-1].Seq {
			t.Fatalf("第 %d 条顺序错误：%d <= %d", i, queued[i].Seq, queued[i-1].Seq)
		}
	}
}
