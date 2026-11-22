package obslog

// P1-3 的可观测面：写入后必须记下"待刷盘字节"，fsync 成功后归零。
//
// 为什么测计数器而不测 fsync 本身：fsync 是系统调用，单测无法证明数据真的
// 离开了易失介质；能守住的只有"该调用的地方确实调用了、且不会每轮空转"。
// 断电语义本身由事件文档写明（窗口 = 最近一轮，退避期最坏 60s）。

import (
	"os"
	"testing"
)

func TestPersistMarksUnsyncedThenSyncClears(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)

	u := newLogUploader()
	u.token, u.endpoint, u.instance = "t", "http://127.0.0.1:1/api/bridge-logs/direct", "inst-sync"

	u.Mu.Lock()
	_, outcome := u.persistLocked(LogEntry{Seq: 1, EventID: "e1", Event: "exec.done"})
	pending := u.unsyncedBytes
	u.syncOutboxLocked()
	cleared := u.unsyncedBytes
	healthy := u.persistOK
	path := u.outboxAt
	u.Mu.Unlock()

	if outcome != persistWritten {
		t.Fatalf("落盘结果应为 persistWritten，实际 %v", outcome)
	}
	if pending <= 0 {
		t.Fatal("写入后 unsyncedBytes 必须 > 0，否则 fsync 会被误跳过")
	}
	if cleared != 0 {
		t.Fatalf("fsync 成功后 unsyncedBytes 应归零，实际 %d", cleared)
	}
	if !healthy {
		t.Fatal("fsync 成功不应把 persistOK 置 false")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("活动段不存在: %v", err)
	}
	if info.Size() != pending {
		t.Fatalf("段大小 %d 与待刷盘字节 %d 不一致（游标按字节前进，二者必须相等）", info.Size(), pending)
	}
}

// 没有脏数据时不得反复调用 Sync（慢盘客户机上的持锁时间是真实成本）。
//
// 阳性对照：故意挂一个已关闭的文件句柄——只要实现真的去 Sync 就会失败并留痕。
// 若哪天有人把 unsyncedBytes 判断挪到 Sync 之后，这里会立刻变红。
func TestSyncOutboxSkipsWhenClean(t *testing.T) {
	dir := t.TempDir()
	withLogDir(t, dir)

	u := newLogUploader()
	u.instance = "inst-clean"

	f, err := os.CreateTemp(dir, "closed-*")
	if err != nil {
		t.Fatalf("建临时文件失败: %v", err)
	}
	path := f.Name()
	if err := f.Close(); err != nil {
		t.Fatalf("关闭临时文件失败: %v", err)
	}

	u.Mu.Lock()
	u.outbox = f // 已关闭：任何 Sync 都会报错
	u.outboxAt = path
	u.unsyncedBytes = 0
	u.syncOutboxLocked()
	failed := u.syncFailedLogged
	u.Mu.Unlock()

	if failed {
		t.Fatal("无脏数据时不应调用 Sync（已关闭句柄的失败留痕说明它被调用了）")
	}
}
