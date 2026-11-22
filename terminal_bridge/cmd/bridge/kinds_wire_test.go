package main

// 链路登记守卫。
//
// 这三个断言合起来回答一个原本无法回答的问题："骨架定义了哪些链路能力，
// 实际每条链路用到哪一层？"——漏登记即红灯，不用再靠读 1600 行的扇出代码。

import (
	"testing"

	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/shared/reconcile/kinds"
)

func TestKindsRegistryCoversEveryKind(t *testing.T) {
	// 用独立实例验证，避免污染包级登记册（其他用例可能依赖它）。
	registry := newKindsRegistryForTest()

	if missing := registry.Missing(); len(missing) != 0 {
		t.Fatalf("以下链路已定义但未登记接入程度：%v", missing)
	}
	for _, kind := range reconcile.AllKinds() {
		if _, ok := registry.Get(kind); !ok {
			t.Fatalf("链路 %s 未登记", kind)
		}
	}
}

func TestKindsRegistryDeclaresAccessMode(t *testing.T) {
	registry := newKindsRegistryForTest()

	for _, d := range registry.All() {
		if !d.Mode.Valid() {
			t.Fatalf("%s 的接入模式 %q 非法", d.Kind, d.Mode)
		}
		if d.Owner == "" || d.Explain == "" {
			t.Fatalf("%s 的 Owner/Explain 不能为空", d.Kind)
		}
		// Note 可以为空，但存在时必须说明"没接的那部分"，便于后续排期。
		t.Logf("%s: mode=%s owner=%s | %s | note=%s", d.Kind, d.Mode, d.Owner, d.Explain, d.Note)
	}
}

// TestKindsRegistryHasExactlyOneIntentOnlySpc 锁死"SPC 是唯一自管写入的链路"。
//
// 这条断言的作用不是禁止，而是让变化必须显式：如果哪天 SPC 接进 Plan/Activate，
// 或又有第二条链路退化成 intent_only，这里会先红，迫使作者更新债务说明。
func TestKindsRegistryHasExactlyOneIntentOnlySpc(t *testing.T) {
	registry := newKindsRegistryForTest()

	var intents []string
	for _, d := range registry.All() {
		if d.Mode == kinds.ModeIntentOnly {
			intents = append(intents, d.Kind)
		}
	}
	if len(intents) != 1 || intents[0] != reconcile.KindSpcSlot {
		t.Fatalf("intent_only 链路应只有 spc_slot，实际 %v", intents)
	}
}

// TestPluginSyncAndLandingAreSeparated 锁死两条插件链路不被再次合并。
//
// 背景：plugin_set（插件同步收敛）此前被误记在 plugin_bundle（安装后落盘校验）上，
// 结果登记册把一条 domain_local 级的链路记成了最弱的 declarative——实际能力比登记强，
// 与 SPC 的 intent_only 债务方向相反、危害相同：没人能判断该看哪儿。
func TestPluginSyncAndLandingAreSeparated(t *testing.T) {
	registry := newKindsRegistryForTest()

	pluginSync, ok := registry.Get(reconcile.KindPluginSet)
	if !ok {
		t.Fatal("plugin_set（插件同步收敛）必须登记")
	}
	if pluginSync.Mode != kinds.ModeDomainLocal {
		t.Fatalf("plugin_set 应登记为 domain_local（自持 Observe/Compare/Act/Receipt），实际 %s", pluginSync.Mode)
	}

	landing, ok := registry.Get(reconcile.KindPluginBundle)
	if !ok {
		t.Fatal("plugin_bundle（安装后落盘校验）必须登记")
	}
	if landing.Mode != kinds.ModeDeclarative {
		t.Fatalf("plugin_bundle 只做只读校验，应登记为 declarative，实际 %s", landing.Mode)
	}
}

// newKindsRegistryForTest 用装配层同一份声明构造独立登记册。
func newKindsRegistryForTest() *kinds.Registry {
	registry := kinds.NewRegistry()
	for _, d := range kindsDescriptors() {
		if err := registry.Register(d); err != nil {
			panic(err)
		}
	}
	return registry
}
