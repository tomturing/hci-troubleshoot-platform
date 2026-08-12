package main

import (
	"path/filepath"
	"testing"

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
	if !simulationBuildable("27123", kbd, 25, "sha256:bundle", "dev_golden", false) {
		t.Fatal("matching published runtime should be buildable")
	}
	if simulationBuildable("27123", kbd, 24, "sha256:bundle", "dev_golden", false) {
		t.Fatal("stale runtime revision was marked buildable")
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
