package session

import (
	"testing"
	"time"
)

// 钉住必须压过空闲回收：日志收集任务几十分钟无人操作是常态，
// 这段时间被 reaper 收走就等于任务中途断链。
func TestPinProtectsIdleSessionFromReap(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())
	h.Release(key)
	// 空闲起点远早于 TTL：没有钉住时必然成为回收候选。
	h.SetSessionIdleAt(key, time.Now().Add(-time.Hour))

	if until := h.Pin(key, 2*time.Hour); until.IsZero() {
		t.Fatal("会话存在时钉住必须返回有效到期时刻")
	}
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("钉住期间不得回收，实际回收 %v", victims)
	}
	// 保护期内必须把空闲起点推到现在：否则钉住一到期就会立刻因"空闲一小时"被回收，
	// 等于把保护窗口变成了"到期即死"。
	h.Unpin(key)
	if victims := h.ReapCandidates(time.Minute); len(victims) != 0 {
		t.Fatalf("解除钉住后仍应保留一整个 TTL 的空闲窗口，实际回收 %v", victims)
	}
}

// 钉住是租约而不是永久保护：到期后必须重新参与回收，否则会话会永久泄漏。
func TestExpiredPinNoLongerProtects(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())
	h.Release(key)
	h.SetSessionIdleAt(key, time.Now().Add(-time.Hour))

	// 用极短租约制造"已过期"：Pin 不接受非正值（那表示关闭钉住）。
	h.Pin(key, time.Nanosecond)
	time.Sleep(2 * time.Millisecond)

	victims := h.ReapCandidates(time.Minute)
	if len(victims) != 1 || victims[0] != key {
		t.Fatalf("钉住过期后必须重新成为回收候选，实际 %v", victims)
	}
	if _, ok := h.PinExpiry(key); ok {
		t.Fatal("过期钉住必须被清理，不得残留在 pins 里")
	}
}

// 对不存在的会话钉住必须无副作用：否则 pins 会积累永远遍历不到的键。
func TestPinIgnoresUnknownSession(t *testing.T) {
	h := NewHub()
	if until := h.Pin("Q404@10.0.0.9", time.Hour); !until.IsZero() {
		t.Fatalf("会话不存在时不得返回到期时刻，实际 %v", until)
	}
	if _, ok := h.PinExpiry("Q404@10.0.0.9"); ok {
		t.Fatal("会话不存在时不得写入钉住记录")
	}
}

// 续期只延长不缩短：短任务的续期请求不能把长任务已有的保护窗口压回去。
func TestPinNeverShortensExistingLease(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())

	long := h.Pin(key, 2*time.Hour)
	short := h.Pin(key, time.Minute)
	if !short.Equal(long) {
		t.Fatalf("续期不得缩短已有保护：期望 %v，实际 %v", long, short)
	}
}

// 销毁必须一并清掉钉住，否则 reaper 会在表里留下无法命中的 pin。
func TestForgetLeaseClearsPin(t *testing.T) {
	h := NewHub()
	key := "Q1@10.0.0.1"
	h.Set(key, newTestSession())
	h.Pin(key, time.Hour)
	h.Remove(key)
	h.ForgetLease(key)

	if _, ok := h.PinExpiry(key); ok {
		t.Fatal("销毁后钉住记录必须被清理")
	}
}
