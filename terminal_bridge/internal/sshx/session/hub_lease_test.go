package session

// 租约语义的边界补充（与 hub_test.go 的基础用例互补）。
//
// 这几条都对应过真实事故形态：保护期内按历史空闲时长回收会腰斩交付，
// 重复 Release 不刷新起点会让"页面刷新"变成"会话倒计时继续走"。

import (
	"testing"
	"time"
)

// TestReleaseIsIdempotentAndRefreshesIdleStart 重复释放必须刷新空闲起点。
//
// 否则第二次 Release（页面刷新、第二条连接接入）不会重置租约，
// 用户刷新一次页面反而让会话更快被回收。
func TestReleaseIsIdempotentAndRefreshesIdleStart(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())

	h.SetSessionIdleAt(key, time.Now().Add(-time.Hour))
	h.Release(key)
	first := h.idleAt[key]
	if time.Since(first) > time.Second {
		t.Fatalf("Release 必须把空闲起点推到当前时刻，实际距今 %s", time.Since(first))
	}

	time.Sleep(10 * time.Millisecond)
	h.Release(key)
	second := h.idleAt[key]
	if !second.After(first) {
		t.Fatal("重复 Release 必须刷新空闲起点（幂等但不清空语义）")
	}
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("刚释放的会话不应立刻被回收，实际 %v", victims)
	}
}

// TestReapCandidatesPushesIdleStartWhileProtected 保护期内必须把起点推到当前时刻。
//
// 这是 2026-10-28 事故的直接成因：拉取结束时保护解除，若 reaper 没刷新 idleAt，
// 下一次扫描会按"终端早已断开"的旧起点立刻回收，交付卡在 uploading。
func TestReapCandidatesPushesIdleStartWhileProtected(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	s := newTestSession()
	h.Set(key, s)
	h.Release(key)
	h.SetSessionIdleAt(key, time.Now().Add(-2*time.Hour))

	s.BeginLongTask()
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("长任务期间不得回收，实际 %v", victims)
	}
	if age := time.Since(h.idleAt[key]); age > time.Second {
		t.Fatalf("保护期内必须刷新空闲起点，实际仍在按 %s 前的起点计时", age)
	}

	// 保护解除后按新起点重新计时：立即扫描不应回收。
	s.EndLongTask()
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("保护刚解除不应立刻回收，实际 %v", victims)
	}
}

// TestReapCandidatesSkipsKeysWithoutLeaseBookkeeping 没有租约簿记的键不参与回收。
//
// 远端 EOF 的会话由 onExit 清理；这里若把无 idleAt 的键也当候选，
// 会与 onExit 形成两条并行的销毁路径（状态双写）。
func TestReapCandidatesSkipsKeysWithoutLeaseBookkeeping(t *testing.T) {
	h := NewHub()
	h.Set("Q1@10.0.0.1", newTestSession())
	h.ForgetLease("Q1@10.0.0.1")
	if victims := h.ReapCandidates(time.Nanosecond); len(victims) != 0 {
		t.Fatalf("无租约簿记的键不应被回收，实际 %v", victims)
	}
}

// TestReapCandidatesNonPositiveTTLDisablesReaping TTL<=0 表示关闭回收。
func TestReapCandidatesNonPositiveTTLDisablesReaping(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())
	h.Release(key)
	h.SetSessionIdleAt(key, time.Now().Add(-24*time.Hour))
	for _, ttl := range []time.Duration{0, -time.Second} {
		if victims := h.ReapCandidates(ttl); len(victims) != 0 {
			t.Fatalf("TTL=%s 应禁用回收，实际 %v", ttl, victims)
		}
	}
}
