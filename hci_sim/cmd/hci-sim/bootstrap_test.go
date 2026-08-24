package main

import (
	"encoding/json"
	"os"
	"path/filepath"
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

func TestBuildSyntheticManifestCarriesQfkVarAsLocalOperationWithoutRoute(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "qfk-var-only", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		VerificationContract: map[string]any{
			"variables": map[string]any{"DESCRIPTION": map[string]any{"type": "string"}},
		},
		LocalOperations: []localOperation{{
			SignalID: "extract-percent", Tool: "qfk_var", Mode: "derive", Operation: "feature_extract",
			Args: map[string]any{
				"schema_version": 1, "mode": "derive", "operation": "feature_extract",
				"input": "{{DESCRIPTION}}", "target_variable": "percent.current",
				"value_type": "percentage", "cardinality": "exactly_one",
			},
			RequiredVariables: []string{"DESCRIPTION"},
			Produces:          []map[string]any{{"name": "CURRENT_PERCENT", "type": "number"}},
		}},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if len(manifest.Routes) != 0 || len(manifest.LocalOperations) != 1 {
		t.Fatalf("qfk_var 不应生成 SSH route: %+v", manifest)
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	if _, err := fixture.Parse(mustJSON(t, manifest)); err != nil {
		t.Fatalf("local operation manifest 必须通过 runtime 校验: %v", err)
	}
}

func TestBuildSyntheticManifestRejectsQfkVarMissingDependency(t *testing.T) {
	_, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "qfk-var-missing", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		LocalOperations: []localOperation{{
			SignalID: "assert-percent", Tool: "qfk_var", Mode: "assert", Operation: "compare",
			Args:              map[string]any{"schema_version": 1, "mode": "assert", "operation": "compare", "left": "{{PERCENT}}", "right": "90%", "operator": ">", "value_type": "percentage"},
			RequiredVariables: []string{"PERCENT"},
		}},
	}, "SIM-HCI-NODE-01", "host")
	if err == nil || !strings.Contains(err.Error(), "PERCENT") {
		t.Fatalf("expected local dependency gap, got %v", err)
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

func TestBuildSyntheticManifestProvidesDeterministicSceneVariables(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "40061", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		SignalsDigest: "sha256:signals", ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{{
			SignalID: "sig_003", Tool: "qfk_log", Argv: []string{"acli", "log", "get", "-t", "{{END}}", "-H", "{{HOST}}"},
			RequiredVariables: []string{"END", "HOST"}, ToolRevision: 1, ToolChecksum: "sha256:tool",
		}},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if got := manifest.Variables["END"]; got != "2026-01-01 00:00:00" {
		t.Fatalf("unexpected deterministic END: %q", got)
	}
	if got := manifest.Variables["HOST"]; got != "SIM-HCI-NODE-01" {
		t.Fatalf("unexpected synthetic HOST: %q", got)
	}
	argv := manifest.Routes[0].RouteKey.Argv
	if argv[4] != "2026-01-01 00:00:00" || argv[6] != "SIM-HCI-NODE-01" {
		t.Fatalf("scene variables were not rendered: %v", argv)
	}
}

func TestBuildSyntheticManifestRendersVariablesFromKbdDerivedOutput(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "23821", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		SignalsDigest: "sha256:signals", ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{{
			SignalID: "sig_001", Tool: "qkv_task", Argv: []string{"acli", "task", "get"},
			ToolRevision: 1, ToolChecksum: "sha256:tool",
			SampleOutput: `{"data":[{"vm":"{{VM}}","host":"{{HOST}}","end":"{{END}}"}]}` + "\n",
		}},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(manifest.Routes[0].Result.Stdout, "{{") {
		t.Fatalf("KBD 派生 stdout 中的变量未渲染: %q", manifest.Routes[0].Result.Stdout)
	}
	if !strings.Contains(manifest.Routes[0].Result.Stdout, "SIM-VM-23821") ||
		!strings.Contains(manifest.Routes[0].Result.Stdout, "SIM-HCI-NODE-01") {
		t.Fatalf("KBD 派生 stdout 未使用受控场景变量: %q", manifest.Routes[0].Result.Stdout)
	}
}

func TestBuildSyntheticManifestRejectsUnknownSceneVariableProvider(t *testing.T) {
	_, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "unknown-variable", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		SignalsDigest: "sha256:signals", ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{{
			SignalID: "sig_unknown", Tool: "qfk_log", Argv: []string{"acli", "log", "get", "-t", "{{CUSTOM_WINDOW}}"},
			RequiredVariables: []string{"CUSTOM_WINDOW"}, ToolRevision: 1, ToolChecksum: "sha256:tool",
		}},
	}, "SIM-HCI-NODE-01", "host")
	if err == nil || !strings.Contains(err.Error(), "未在已发布 KBD Contract 或 Producer 中声明") || !strings.Contains(err.Error(), "CUSTOM_WINDOW") {
		t.Fatalf("expected unknown scene variable gap, got %v", err)
	}
}

func TestBuildSyntheticManifestUsesPublishedContractForCustomVariable(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "18906", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64),
		SignalsDigest: "sha256:signals", ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		VerificationContract: map[string]any{
			"variables": map[string]any{"VM_DISK_ID": map[string]any{"type": "string"}},
		},
		SyntheticRoutes: []syntheticRoute{{
			SignalID: "expert_1787040047488_900be1862f6d", Tool: "qfk_vm",
			Argv:              []string{"acli", "vm", "disk", "get", "{{VM_DISK_ID}}"},
			RequiredVariables: []string{"VM_DISK_ID"}, ToolRevision: 1, ToolChecksum: "sha256:tool",
		}},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if got := manifest.Variables["VM_DISK_ID"]; got != "SIM-vm-disk-id-18906" {
		t.Fatalf("unexpected declared variable binding: %q", got)
	}
	if got := manifest.Routes[0].RouteKey.Argv[4]; got != manifest.Variables["VM_DISK_ID"] {
		t.Fatalf("argv did not use declared binding: %q", got)
	}
}

func TestBuildSyntheticManifestUsesPublishedProducerForCustomVariable(t *testing.T) {
	manifest, err := buildSyntheticManifest(&resolvedKbd{
		SupportID: "producer-case", KBDRevision: 1, KBDChecksum: strings.Repeat("b", 64),
		SignalsDigest: "sha256:signals", ToolContractRevision: "tool-r1", PolicyRevision: "policy-r1",
		SyntheticRoutes: []syntheticRoute{
			{SignalID: "producer", Tool: "qkv_task", Argv: []string{"acli", "task", "get"},
				ToolRevision: 1, ToolChecksum: "sha256:tool",
				Produces: []map[string]any{{"name": "VM_DISK_ID", "type": "string"}}},
			{SignalID: "consumer", Tool: "qfk_vm", Argv: []string{"acli", "vm", "disk", "get", "{{VM_DISK_ID}}"},
				RequiredVariables: []string{"VM_DISK_ID"}, ToolRevision: 1, ToolChecksum: "sha256:tool"},
		},
	}, "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatal(err)
	}
	if got := manifest.Routes[1].RouteKey.Argv[4]; got != "SIM-vm-disk-id-producer-case" {
		t.Fatalf("producer binding was not used: %q", got)
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

func TestBuildScenarioManifestRendersVariablesAndAllVariants(t *testing.T) {
	resolved := &resolvedKbd{
		SupportID: "SAMPLE-DYNAMIC", KBDRevision: 2, KBDChecksum: strings.Repeat("a", 64),
		SignalsDigest: "sha256:signals", ToolContractRevision: "tool-r2", PolicyRevision: "policy-r2",
		Metadata: map[string]any{"sample_suite": "suite-v1"},
		SyntheticRoutes: []syntheticRoute{{
			SignalID: "sig-vm", Tool: "qfk_vm", Role: "must",
			Argv:         []string{"acli", "--formatter", "json", "vm", "status", "get", "--vm-id", "{{VM_ID}}"},
			ToolRevision: 2, ToolChecksum: "sha256:tool", RequiredVariables: []string{"VM_ID"},
		}},
	}
	profile := &scenarioProfile{
		SchemaVersion: "1.0", SampleSuite: "suite-v1", Variables: map[string]string{"HOST": "SIM-NODE"},
		Cases: map[string]scenarioCaseProfile{
			"SAMPLE-DYNAMIC": {
				ProductVersion: "6.12.0", Variables: map[string]string{"VM_ID": "9001"},
				Signals: map[string]scenarioSignal{"sig-vm": {PositiveOutput: `{"data":[{"id":"{{VM_ID}}"}]}` + "\n", NegativeOutput: `{"data":[]}` + "\n"}},
			},
		},
	}
	for _, variant := range []string{"positive", "negative", "missing-evidence", "command-failed", "timeout", "version-incompatible"} {
		manifest, err := buildScenarioManifest(resolved, profile, "SIM-NODE", "host", variant)
		if err != nil {
			t.Fatalf("variant=%s: %v", variant, err)
		}
		route := manifest.Routes[0]
		if route.Variant != variant || route.RouteKey.Argv[len(route.RouteKey.Argv)-1] != "9001" {
			t.Fatalf("variant=%s route=%+v", variant, route)
		}
		if variant == "positive" && !strings.Contains(route.Result.Stdout, `"9001"`) {
			t.Fatalf("positive output did not render variables: %q", route.Result.Stdout)
		}
		if variant == "timeout" && route.Fault.Type != fixture.FaultTimeout {
			t.Fatalf("timeout fault missing: %+v", route.Fault)
		}
	}
}

func TestBuildScenarioManifestFailsClosedForMissingVariableAndOutput(t *testing.T) {
	resolved := &resolvedKbd{
		SupportID: "SAMPLE", KBDRevision: 1, KBDChecksum: strings.Repeat("a", 64), SignalsDigest: "sha256:s",
		ToolContractRevision: "tool", PolicyRevision: "policy", Metadata: map[string]any{"sample_suite": "suite"},
		SyntheticRoutes: []syntheticRoute{{SignalID: "sig", Tool: "qfk_vm", Argv: []string{"acli", "vm", "get", "{{VM_ID}}"}, ToolRevision: 1, ToolChecksum: "sha256:t"}},
	}
	profile := &scenarioProfile{SchemaVersion: "1.0", SampleSuite: "suite", Cases: map[string]scenarioCaseProfile{"SAMPLE": {Signals: map[string]scenarioSignal{"sig": {PositiveOutput: "ok\n"}}}}}
	if _, err := buildScenarioManifest(resolved, profile, "SIM", "host", "positive"); err == nil || !strings.Contains(err.Error(), "缺少场景变量") {
		t.Fatalf("expected variable gap, got %v", err)
	}
	delete(profile.Cases["SAMPLE"].Signals, "sig")
	raw, err := json.Marshal(profile)
	if err != nil {
		t.Fatal(err)
	}
	profilePath := filepath.Join(t.TempDir(), "profile.json")
	if err := os.WriteFile(profilePath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadScenarioProfile(profilePath, resolved); err == nil || !strings.Contains(err.Error(), "缺少 Signal") {
		t.Fatalf("expected output gap, got %v", err)
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
