package kinds

import (
	"strings"
	"testing"

	"terminal_bridge/internal/shared/reconcile"
)

func validDescriptor() Descriptor {
	return Descriptor{
		Kind:    reconcile.KindConfigFile,
		Mode:    ModePlanActivate,
		Owner:   "test",
		Explain: "测试链路",
	}
}

func TestRegisterRejectsInvalidDescriptor(t *testing.T) {
	cases := []struct {
		name string
		mut  func(*Descriptor)
		want string
	}{
		{"未知 Kind", func(d *Descriptor) { d.Kind = "not_a_kind" }, "未登记的 Kind"},
		{"非法 Mode", func(d *Descriptor) { d.Mode = "bogus" }, "Mode"},
		{"缺 Owner", func(d *Descriptor) { d.Owner = "  " }, "缺少 Owner"},
		{"缺 Explain", func(d *Descriptor) { d.Explain = "" }, "缺少 Explain"},
	}
	for _, c := range cases {
		r := NewRegistry()
		d := validDescriptor()
		c.mut(&d)
		err := r.Register(d)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Fatalf("%s: 期望包含 %q 的错误，实际 %v", c.name, c.want, err)
		}
	}
}

func TestRegisterRejectsDuplicate(t *testing.T) {
	r := NewRegistry()
	d := validDescriptor()
	if err := r.Register(d); err != nil {
		t.Fatal(err)
	}
	if err := r.Register(d); err == nil {
		t.Fatal("重复登记必须失败，否则后写者会静默覆盖前者的接入声明")
	}
}

func TestMissingReportsUnregisteredKinds(t *testing.T) {
	r := NewRegistry()
	missing := r.Missing()
	if len(missing) != len(reconcile.AllKinds()) {
		t.Fatalf("空登记册应报告全部 %d 条缺失，实际 %v", len(reconcile.AllKinds()), missing)
	}
	if err := r.Register(validDescriptor()); err != nil {
		t.Fatal(err)
	}
	for _, kind := range r.Missing() {
		if kind == reconcile.KindConfigFile {
			t.Fatal("已登记的链路不应出现在 Missing 中")
		}
	}
}

func TestAllReturnsSortedCopy(t *testing.T) {
	r := NewRegistry()
	for _, kind := range []string{reconcile.KindSpcSlot, reconcile.KindAcliBinary} {
		if err := r.Register(Descriptor{Kind: kind, Mode: ModeIntentOnly, Owner: "t", Explain: "e"}); err != nil {
			t.Fatal(err)
		}
	}
	all := r.All()
	if len(all) != 2 || all[0].Kind > all[1].Kind {
		t.Fatalf("All 必须按 Kind 排序，实际 %v", all)
	}
	// 返回值是副本：外部修改不得影响登记册。
	all[0].Mode = ModeDeclarative
	again := r.All()
	if again[0].Mode == ModeDeclarative {
		t.Fatal("All 必须返回副本，否则调用方可静默篡改链路声明")
	}
}

func TestModeValid(t *testing.T) {
	for _, m := range []Mode{ModePlanActivate, ModeCompareOnly, ModeIntentOnly, ModeDeclarative} {
		if !m.Valid() {
			t.Fatalf("%s 应合法", m)
		}
	}
	if Mode("whatever").Valid() {
		t.Fatal("未知 Mode 不应合法")
	}
}

func TestResetClearsRegistry(t *testing.T) {
	r := NewRegistry()
	if err := r.Register(validDescriptor()); err != nil {
		t.Fatal(err)
	}
	r.Reset()
	if len(r.All()) != 0 {
		t.Fatal("Reset 后登记册应为空")
	}
}
