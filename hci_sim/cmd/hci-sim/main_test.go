package main

import "testing"

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
