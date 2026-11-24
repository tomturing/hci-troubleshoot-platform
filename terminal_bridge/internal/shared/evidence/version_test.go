package evidence

import "testing"

// 注入后必须能原样读出，且两侧空白要去掉（ldflags 注入串常带首尾空白）。
func TestSetBridgeVersionTrimsAndReads(t *testing.T) {
	t.Cleanup(func() { SetBridgeVersion("") })
	SetBridgeVersion("  v2.37.0-13-g7ea91e5d  ")
	if got := BridgeVersion(); got != "v2.37.0-13-g7ea91e5d" {
		t.Fatalf("BridgeVersion()=%q，期望去掉空白后的版本串", got)
	}
}

// 未注入时必须返回空串：调用方据此省略请求头，而不是发一个空头。
func TestBridgeVersionEmptyByDefault(t *testing.T) {
	t.Cleanup(func() { SetBridgeVersion("") })
	SetBridgeVersion("")
	if got := BridgeVersion(); got != "" {
		t.Fatalf("未注入时 BridgeVersion()=%q，期望空串", got)
	}
}

// 清空必须真的清掉：否则测试之间会互相污染，线上也只能重启才复位。
func TestSetBridgeVersionCanBeCleared(t *testing.T) {
	t.Cleanup(func() { SetBridgeVersion("") })
	SetBridgeVersion("v2.37.0-13")
	SetBridgeVersion("")
	if got := BridgeVersion(); got != "" {
		t.Fatalf("清空后 BridgeVersion()=%q，期望空串", got)
	}
}
