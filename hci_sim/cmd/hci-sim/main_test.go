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
