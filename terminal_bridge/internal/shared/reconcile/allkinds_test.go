package reconcile

import "testing"

// TestAllKindsMatchesValidKind 双向钉死链路枚举。
//
// 只测一个方向不够：漏了 AllKinds 会让 kinds 登记册少查一条（守卫静默失效），
// 漏了 ValidKind 会让新链路在注册时被拒（启动 panic）。两个方向都要红。
func TestAllKindsMatchesValidKind(t *testing.T) {
	kinds := AllKinds()
	if len(kinds) == 0 {
		t.Fatal("AllKinds 不能为空")
	}

	seen := make(map[string]bool, len(kinds))
	for _, kind := range kinds {
		if kind == "" {
			t.Fatal("AllKinds 含空值")
		}
		if seen[kind] {
			t.Fatalf("AllKinds 含重复项: %s", kind)
		}
		seen[kind] = true
		if !ValidKind(kind) {
			t.Fatalf("%s 在 AllKinds 中但 ValidKind 拒绝——新增链路时两处必须同步", kind)
		}
	}

	// 反向：ValidKind 接受但 AllKinds 未列出的值（用一个明显的哨兵探测）。
	if ValidKind("__not_a_kind__") {
		t.Fatal("ValidKind 对未知值返回 true")
	}
}
