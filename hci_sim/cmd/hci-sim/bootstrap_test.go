package main

import (
	"encoding/json"
	"strings"
	"testing"

	"hci_sim/internal/fixture"
)

func TestBuildSyntheticManifestIsExactAndSynthetic(t *testing.T) {
	manifest := buildSyntheticManifest(&resolvedKbd{
		SupportID: "27736", KBDRevision: 1, KBDChecksum: "e2a4d1761206b1dbe3bcd19329af329cabaf2094f59aa3f70a81ebbebb55f74e",
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
	}, syntheticCatalog["27736"], "SIM-HCI-NODE-01", "host")
	if manifest.Bundle.Status != "published" || manifest.KBD.SupportID != "27736" || len(manifest.Routes) != 1 {
		t.Fatalf("unexpected manifest: %+v", manifest)
	}
	if manifest.Routes[0].Variant != "positive-minimal" || manifest.Routes[0].RouteKey.Tool != "qkv_task" {
		t.Fatalf("route is not synthetic minimal: %+v", manifest.Routes[0])
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	if _, err := fixture.Parse(mustJSON(t, manifest)); err != nil {
		t.Fatalf("generated manifest must pass runtime validation: %v", err)
	}
}

func TestKBD23821SyntheticRouteUsesPublishedTaskContract(t *testing.T) {
	route, ok := syntheticCatalog["23821"]
	if !ok {
		t.Fatal("KBD23821 must be explicitly registered for synthetic acceptance")
	}
	if route.Keyword != "迁移虚拟机" || route.Limit != "1" {
		t.Fatalf("unexpected KBD23821 route: %+v", route)
	}
	manifest := buildSyntheticManifest(&resolvedKbd{
		SupportID: "23821", KBDRevision: 2, KBDChecksum: "a" + strings.Repeat("b", 63),
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
	}, route, "SIM-HCI-NODE-01", "host")
	if got := manifest.Routes[0].RouteKey.Argv; len(got) != 7 || got[0] != "qkv_task" || got[2] != "迁移虚拟机" || got[4] != "1" {
		t.Fatalf("unexpected KBD23821 argv: %v", got)
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
