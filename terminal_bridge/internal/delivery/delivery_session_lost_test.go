package delivery

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"terminal_bridge/internal/sshx"
)

// TestWaitCollectNoSessionFailsFast 守护：no-session 是确定的会话失效，
// 必须立即交回重排，而不是像"节点忙"那样累计到 3 次。
//
// 现场形态（Q2026112500592）：每轮 3 次 × 60s 全撞同一个僵尸会话，
// 5 轮预算在 44 分钟里被白白烧光。立即返回能把失败前移到第一次探测，
// 让外层 sessionFor 尽快重建会话。
func TestWaitCollectNoSessionFailsFast(t *testing.T) {
	var calls int32
	session := ptySessionFunc{run: func(context.Context, string, time.Duration) (string, int, error) {
		atomic.AddInt32(&calls, 1)
		return "", -1, sshx.ErrNoSession
	}}
	r := &deliveryRunner{}
	job := &deliveryJob{JobID: "dj-test", TaskID: "task-test", CaseID: "case-x", NodeIP: "10.0.0.1", Phase: phaseWaitingCollect}

	err := r.waitCollect(context.Background(), session, job)
	if err == nil {
		t.Fatal("no-session 必须立即返回错误，交回重排")
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("no-session 应在第一次探测后立即返回，实际调用 %d 次", got)
	}
}

// TestIsNodeSessionLost 锁定文案契约：交付域不 import sshx 具体包，
// 靠 ErrNoSession 的文案识别；文案一旦改动必须同步 errSessionLostText。
func TestIsNodeSessionLost(t *testing.T) {
	if !isNodeSessionLost(sshx.ErrNoSession) {
		t.Fatal("sshx.ErrNoSession 必须被 isNodeSessionLost 识别")
	}
	if isNodeSessionLost(nil) {
		t.Fatal("nil 不应被判为会话失效")
	}
}

// ptySessionFunc 只实现 RunCommandPty 与 Alive，其余 NodeSession 能力用内嵌接口占位。
type ptySessionFunc struct {
	NodeSession
	run func(context.Context, string, time.Duration) (string, int, error)
}

func (p ptySessionFunc) RunCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error) {
	return p.run(ctx, command, timeout)
}

func (ptySessionFunc) Alive() bool { return true }
