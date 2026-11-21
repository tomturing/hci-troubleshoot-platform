package session

import (
	"testing"
	"time"

	"terminal_bridge/internal/sshx"
)

func newTestSession() *sshx.SSHSession {
	return &sshx.SSHSession{Done: make(chan struct{}), CreatedAt: time.Now()}
}

// 入表即持有：Set 必须同时登记 refs=1，否则会话一进表就是"无人持有"，
// reaper 在下一个 tick 就能按空闲 TTL 把它回收掉（租约语义的根基）。
func TestSetRegistersHolderSoReaperSkips(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())

	// 把空闲起点推到一小时前也不该被回收：refs=1 优先于空闲时长。
	h.SetSessionIdleAt(key, time.Now().Add(-time.Hour))
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("有持有者的会话不得被回收，实际回收 %v", victims)
	}
}

// 释放是"降级"而不是"销毁"：归零后会话仍在表内，只是进入空闲租约。
func TestReleaseKeepsSessionUntilLeaseExpires(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	sess := newTestSession()
	h.Set(key, sess)

	if !h.Release(key) {
		t.Fatal("唯一持有者释放后必须归零")
	}
	if h.Get(key) != sess || h.Count() != 1 {
		t.Fatal("释放不得销毁会话：刷新页面/面板借用都依赖复用")
	}

	// 刚归零不算过期；空闲超过 TTL 才进入回收候选。
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("刚进入空闲租约不应立即回收，实际 %v", victims)
	}
	h.SetSessionIdleAt(key, time.Now().Add(-2*time.Minute))
	victims := h.ReapCandidates(time.Minute)
	if len(victims) != 1 || victims[0] != key {
		t.Fatalf("空闲超时后应成为回收候选，实际 %v", victims)
	}
}

// 销毁必须清掉租约簿记，否则空闲表里会留下幽灵键，reaper 每轮都命中它。
func TestForgetLeaseClearsBookkeeping(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())
	h.Release(key)
	h.SetSessionIdleAt(key, time.Now().Add(-time.Hour))

	h.Remove(key)
	h.ForgetLease(key)
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("簿记清理后不得再有回收候选，实际 %v", victims)
	}
}

// ttl<=0 表示关闭租约回收（退化为"永不主动销毁"），必须直接返回空。
func TestReapCandidatesDisabledByNonPositiveTTL(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())
	h.Release(key)
	h.SetSessionIdleAt(key, time.Now().Add(-24*time.Hour))

	if victims := h.ReapCandidates(0); len(victims) != 0 {
		t.Fatalf("租约关闭时不得回收，实际 %v", victims)
	}
}
