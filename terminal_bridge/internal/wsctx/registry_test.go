package wsctx

import (
	"testing"

	"terminal_bridge/internal/shared/models"
)

// 测试用路由一律带 t- 前缀，避免与真实消息类型撞名（本包测试与装配层 init 可能同进程）。
const (
	tRouteA = "t-registry-a"
	tRouteB = "t-registry-b"
)

func noopHandler(*Context, models.InMessage) {}

// 注册后必须能取回同一个处理器：域自注册的价值就是 Lookup 能找到它。
func TestRegisterAndLookup(t *testing.T) {
	Register(tRouteA, noopHandler)
	if got := Lookup(tRouteA); got == nil {
		t.Fatalf("注册后 Lookup(%q) 应返回 handler", tRouteA)
	}
}

// 未知类型必须静默返回 nil，不得 panic、不得误派发到相近类型。
func TestLookupUnknownReturnsNil(t *testing.T) {
	for _, typ := range []string{"", "t-registry-missing", "T-REGISTRY-A"} {
		if h := Lookup(typ); h != nil {
			t.Fatalf("未知类型 %q 不应有路由", typ)
		}
	}
}

// 重复注册必须 fail-fast：两个域争抢同一消息时不能静默覆盖。
func TestRegisterDuplicatePanics(t *testing.T) {
	Register(tRouteB, noopHandler)
	defer func() {
		if recover() == nil {
			t.Fatal("重复注册应 panic")
		}
	}()
	Register(tRouteB, noopHandler)
}

// 批量注册是原子的：任一条非法则一条都不进表，避免"半个域在线"。
func TestRegisterRoutesIsAtomic(t *testing.T) {
	const typ = "t-registry-half"
	defer func() {
		if recover() == nil {
			t.Fatal("nil handler 应 panic")
		}
		if h := Lookup(typ); h != nil {
			t.Fatalf("批量注册中途失败时 %q 不应进表", typ)
		}
	}()
	RegisterRoutes([]Route{
		{Type: typ, Handler: noopHandler},
		{Type: "t-registry-nil", Handler: nil},
	})
}

// 同批内两条同类型也必须拦下，否则后者静默覆盖前者。
func TestRegisterRoutesDuplicateInBatchPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("同批重复类型应 panic")
		}
	}()
	RegisterRoutes([]Route{
		{Type: "t-registry-dup", Handler: noopHandler},
		{Type: "t-registry-dup", Handler: noopHandler},
	})
}

// Snapshot 必须是副本：调用方改返回值不能污染全局表（否则可绕过重复检查）。
func TestSnapshotIsCopy(t *testing.T) {
	Register("t-registry-snap", noopHandler)
	snap := Snapshot()
	delete(snap, "t-registry-snap")
	snap["t-registry-injected"] = noopHandler
	if Lookup("t-registry-snap") == nil {
		t.Fatal("删除快照条目不应影响全局表")
	}
	if Lookup("t-registry-injected") != nil {
		t.Fatal("往快照里塞条目不应进入全局表")
	}
}
