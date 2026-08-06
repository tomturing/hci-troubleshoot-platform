package main

import (
	"encoding/json"
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

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
