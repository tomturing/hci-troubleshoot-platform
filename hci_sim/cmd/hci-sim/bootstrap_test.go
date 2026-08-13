package main

import (
	"encoding/json"
	"strings"
	"testing"

	"hci_sim/internal/fixture"
)

func TestBuildSyntheticManifestIsExactAndSynthetic(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "27736", KBDRevision: 1, KBDChecksum: "e2a4d1761206b1dbe3bcd19329af329cabaf2094f59aa3f70a81ebbebb55f74e",
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{{SignalID: "sig-task", Tool: "qkv_task", Argv: []string{"acli", "--formatter", "json", "task", "get", "-k", "设置集群IP失败", "-l", "100"}, ToolRevision: 3, ToolChecksum: "sha256:tool"}},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if manifest.Bundle.Status != "published" || manifest.KBD.SupportID != "27736" || len(manifest.Routes) != 1 {
		t.Fatalf("unexpected manifest: %+v", manifest)
	}
	if manifest.Routes[0].Variant != "positive-minimal" || manifest.Routes[0].RouteKey.Tool != "acli" {
		t.Fatalf("route is not synthetic minimal: %+v", manifest.Routes[0])
	}
	if manifest.Routes[0].ToolRevision != 3 || manifest.Routes[0].ToolChecksum != "sha256:tool" {
		t.Fatalf("route did not preserve Tool revision: %+v", manifest.Routes[0])
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	if _, err := fixture.Parse(mustJSON(t, manifest)); err != nil {
		t.Fatalf("generated manifest must pass runtime validation: %v", err)
	}
}

func TestBuildSyntheticManifestAcceptsAnyPublishedKBDContract(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "new-kbd-without-code-change", KBDRevision: 2, KBDChecksum: "a" + strings.Repeat("b", 63),
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{{SignalID: "signal-dynamic", Tool: "qkv_task", Argv: []string{"acli", "--formatter", "json", "task", "get", "-k", "动态关键词", "-l", "1"}, ToolRevision: 4, ToolChecksum: "sha256:tool"}},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if got := manifest.Routes[0].RouteKey.Argv; len(got) != 9 || got[0] != "acli" || got[6] != "动态关键词" || got[8] != "1" {
		t.Fatalf("unexpected KBD23821 argv: %v", got)
	}
}

func TestBuildSyntheticManifestRejectsUncontrolledCommand(t *testing.T) {
	_, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "dynamic", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{{SignalID: "sig", Tool: "qfk_system", Argv: []string{"bash", "-c", "id"}, ToolRevision: 1, ToolChecksum: "sha256:tool"}},
	}, "SIM-HCI-NODE-01", "host")
	if err == nil || !strings.Contains(err.Error(), "不是受控 aCLI") {
		t.Fatalf("expected controlled aCLI rejection, got %v", err)
	}
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
