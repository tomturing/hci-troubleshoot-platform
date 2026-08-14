package main

import (
	"testing"

	"hci_sim/internal/fixture"
)

func TestCommandSemanticKeySeparatesSampleSignalCommands(t *testing.T) {
	tests := map[string][]string{
		"task:get:-k=启动虚拟机":              {"acli", "--formatter", "json", "task", "get", "-k", "启动虚拟机"},
		"vm:status:get":                  {"acli", "--formatter", "json", "vm", "status", "get", "--vm-id", "9001"},
		"vm:list":                        {"acli", "--formatter", "json", "vm", "list"},
		"service:asv:asv-manager:status": {"acli", "service", "asv", "asv-manager", "status"},
		"system:df":                      {"acli", "--cluster", "--timeout", "90", "system", "df", "-P", "/sf/log"},
		"network:nic:list":               {"acli", "--formatter", "keyvalue", "network", "nic", "list"},
	}
	for expected, argv := range tests {
		if got := commandSemanticKey(argv); got != expected {
			t.Fatalf("argv=%q got=%s want=%s", argv, got, expected)
		}
	}
}

func TestMatchOfflineResultAllowsOnlineOfflineOptionDifferencesButRejectsAmbiguity(t *testing.T) {
	results := []scenarioRouteResult{
		{argv: []string{"acli", "log", "get", "-k", "drop", "-i", "REQ"}, result: fixture.Result{FixtureID: "log-1", SignalID: "sig-log"}},
		{argv: []string{"acli", "vm", "list"}, result: fixture.Result{FixtureID: "vm", SignalID: "sig-vm"}},
	}
	result, ok := matchOfflineResult("SAMPLE", nil, []string{"acli", "log", "get", "-k", "drop"}, results)
	if !ok || result.SignalID != "sig-log" {
		t.Fatalf("result=%+v ok=%v", result, ok)
	}
	results = append(results, scenarioRouteResult{argv: []string{"acli", "log", "get", "-k", "drop", "-i", "OTHER"}, result: fixture.Result{FixtureID: "log-2"}})
	if _, ok := matchOfflineResult("SAMPLE", nil, []string{"acli", "log", "get", "-k", "drop"}, results); ok {
		t.Fatal("ambiguous log routes must fail closed")
	}
}

func TestMatchOfflineResultPrefersSignedSignalRef(t *testing.T) {
	results := []scenarioRouteResult{
		{argv: []string{"acli", "log", "get", "-k", "old"}, result: fixture.Result{FixtureID: "fixture-a", SignalID: "sig-a"}},
		{argv: []string{"acli", "log", "get", "-k", "new"}, result: fixture.Result{FixtureID: "fixture-b", SignalID: "sig-b"}},
	}
	refs := []offlineSourceSignalRef{{SupportID: "SAMPLE", SignalID: "sig-b"}}
	result, ok := matchOfflineResult("SAMPLE", refs, []string{"acli", "log", "get", "-k", "different"}, results)
	if !ok || result.SignalID != "sig-b" {
		t.Fatalf("签名 Signal 绑定未优先使用: result=%+v ok=%v", result, ok)
	}
}
