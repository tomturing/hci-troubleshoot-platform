package main

import (
	"path/filepath"
	"testing"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/fixture"
)

func TestSimulationFixtureVariantDefaultsToSynthetic(t *testing.T) {
	t.Setenv("HCI_SIM_FIXTURE_VARIANT", "")
	if got := simulationFixtureVariant(); got != "positive-minimal" {
		t.Fatalf("default fixture variant = %q, want positive-minimal", got)
	}
}

func TestSimulationFixtureVariantUsesRuntimeConfiguration(t *testing.T) {
	t.Setenv("HCI_SIM_FIXTURE_VARIANT", "positive-realistic")
	if got := simulationFixtureVariant(); got != "positive-realistic" {
		t.Fatalf("configured fixture variant = %q, want positive-realistic", got)
	}
}

func TestDigestValueIsStableAndContentAddressed(t *testing.T) {
	first := digestValue(map[string]any{"kbd_id": "27123", "variant": "positive-realistic"})
	second := digestValue(map[string]any{"kbd_id": "27123", "variant": "positive-realistic"})
	if first != second {
		t.Fatalf("digest is not stable: %q != %q", first, second)
	}
	if len(first) != len("sha256:")+64 || first[:len("sha256:")] != "sha256:" {
		t.Fatalf("unexpected digest format: %q", first)
	}
	if first == digestValue(map[string]any{"kbd_id": "23821", "variant": "positive-realistic"}) {
		t.Fatal("different content unexpectedly has the same digest")
	}
}

func TestJSONIntRejectsFractionalAndWrongTypes(t *testing.T) {
	if got, ok := jsonInt(float64(27123)); !ok || got != 27123 {
		t.Fatalf("integer JSON number was rejected: %d %v", got, ok)
	}
	if _, ok := jsonInt(float64(1.5)); ok {
		t.Fatal("fractional JSON number was accepted as revision")
	}
	if _, ok := jsonInt("27123"); ok {
		t.Fatal("string revision was accepted")
	}
}

func TestSimulationEnvironmentContextCarriesAgentScopeAndBinding(t *testing.T) {
	router, err := fixture.Load(filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json"))
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	t.Setenv("HCI_SIM_SSH_HOST", "hci-sim.example.svc")
	context := simulationEnvironmentContext(router, "run-27123", "Q27123", "positive-realistic")
	for key, want := range map[string]any{
		"simulation": true, "execution_mode": "sim-ssh", "test_run_id": "run-27123",
		"case_id": "Q27123", "support_id": "27123", "product": "HCI",
		"node_ip": "hci-sim.example.svc",
	} {
		if context[key] != want {
			t.Fatalf("context[%q] = %#v, want %#v", key, context[key], want)
		}
	}
	components, ok := context["components"].([]string)
	if !ok || len(components) != 1 || components[0] != "虚拟机" {
		t.Fatalf("unexpected components: %#v", context["components"])
	}
}

func TestSimulationBuildableRequiresExactActiveRevision(t *testing.T) {
	kbd := fixture.KBDRef{SupportID: "27123", Revision: 25, Checksum: "sha256:kbd"}
	if !simulationBuildable("27123", kbd, 25, "sha256:bundle", "dev_golden", false, "internal_fast") {
		t.Fatal("matching published runtime should be buildable")
	}
	if simulationBuildable("27123", kbd, 24, "sha256:bundle", "dev_golden", false, "internal_fast") {
		t.Fatal("stale runtime revision was marked buildable")
	}
}

func TestSimulationBuildableAllowsKBDDerivedFixtureOnlyInInternalFastPath(t *testing.T) {
	kbd := fixture.KBDRef{SupportID: "27123", Revision: 25}
	if !simulationBuildable("27123", kbd, 25, "sha256:bundle", "dev_golden", true, "internal_fast") {
		t.Fatal("internal_fast must allow KBD-derived fixtures")
	}
	if simulationBuildable("27123", kbd, 25, "sha256:bundle", "dev_golden", true, "high_assurance") {
		t.Fatal("high_assurance must reject synthetic fixtures")
	}
}

func TestPublishedBundleInputsAreStableAndComplete(t *testing.T) {
	pool, err := fixture.LoadBundlePool(
		filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json"),
		filepath.Join("..", "..", "..", "deploy", "helm", "hci-sim", "files", "kbd-23821-fixture-manifest.json"),
	)
	if err != nil {
		t.Fatalf("load bundle pool: %v", err)
	}
	inputs := publishedBundleInputs(pool, "positive-realistic")
	if len(inputs) != 2 || inputs[0].SupportID != "23821" || inputs[1].SupportID != "27123" {
		t.Fatalf("published inputs are not stably sorted: %+v", inputs)
	}
	for _, input := range inputs {
		router := pool.Get(input.SupportID)
		if input.Digest != router.BundleDigest() || input.ObjectDigest != router.ManifestHash() || input.SizeBytes != router.ManifestSize() {
			t.Fatalf("published input does not preserve bundle identity: %+v", input)
		}
		wantURI := "configmap://hci-sim-fixture/kbd-" + input.SupportID + "-fixture-manifest.json"
		if input.ObjectURI != wantURI {
			t.Fatalf("object URI = %q, want %q", input.ObjectURI, wantURI)
		}
		wantFingerprint := digestValue(map[string]any{
			"support_id": input.SupportID, "kbd_revision": input.KBDRevision,
			"variant": input.Variant, "bundle_digest": input.Digest,
		})
		if input.InputFingerprint != wantFingerprint {
			t.Fatalf("input fingerprint = %q, want %q", input.InputFingerprint, wantFingerprint)
		}
	}
}

func TestRuntimeBundleMetadataUsesFrozenRegistryIdentity(t *testing.T) {
	router, err := fixture.Load(filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json"))
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	record := controlplane.BundleRecord{
		Digest:           router.BundleDigest(),
		InputFingerprint: "sha256:compiled-input-fingerprint",
		Input:            controlplane.CompileInput{SupportID: router.KBD().SupportID, KBDRevision: router.KBD().Revision},
		Object:           controlplane.ObjectRef{Key: "bundle-object/27123", Digest: router.ManifestHash(), Size: router.ManifestSize()},
	}
	metadata, err := runtimeBundleMetadataFromRecord(router, record)
	if err != nil {
		t.Fatalf("resolve metadata: %v", err)
	}
	if metadata.InputFingerprint != record.InputFingerprint || metadata.ObjectURI != record.Object.Key || metadata.ObjectDigest != router.ManifestHash() || metadata.SizeBytes != router.ManifestSize() {
		t.Fatalf("metadata did not preserve registry identity: %+v", metadata)
	}
}

func TestRuntimeBundleMetadataRejectsDigestConflict(t *testing.T) {
	router, err := fixture.Load(filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json"))
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	_, err = runtimeBundleMetadataFromRecord(router, controlplane.BundleRecord{
		Digest:           "sha256:other",
		InputFingerprint: "sha256:compiled-input-fingerprint",
		Input:            controlplane.CompileInput{SupportID: router.KBD().SupportID, KBDRevision: router.KBD().Revision},
		Object:           controlplane.ObjectRef{Key: "bundle-object/27123", Digest: router.ManifestHash(), Size: router.ManifestSize()},
	})
	if err == nil {
		t.Fatal("digest conflict must be rejected")
	}
}

func TestTerminalRunStatus(t *testing.T) {
	for _, status := range []string{"passed", "failed", "inconclusive", "cancelled", "expired"} {
		if !terminalRunStatus(status) {
			t.Fatalf("status %q should be terminal", status)
		}
	}
	for _, status := range []string{"requested", "leased", "preparing", "running"} {
		if terminalRunStatus(status) {
			t.Fatalf("status %q should remain active", status)
		}
	}
}
