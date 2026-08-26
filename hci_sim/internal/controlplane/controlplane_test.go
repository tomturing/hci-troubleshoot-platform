package controlplane

import (
	"context"
	"strings"
	"testing"
	"time"

	"hci_sim/internal/fixture"
)

func fixtureManifest() fixture.Manifest {
	manifest := fixture.Manifest{
		SchemaVersion: fixture.SchemaVersion,
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: "27123", Revision: 24, Checksum: "sha256:kbd-27123"},
		Contracts:     fixture.Contracts{ToolRevision: "tool-r24", PolicyRevision: "policy-r2"},
		Limits:        fixture.Limits{MaxRoutes: 8, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes: []fixture.Route{{
			ID: "signal-1", SignalID: "sig-001", Variant: "positive-realistic",
			RouteKey: fixture.RouteKey{Tool: "acli", AcquisitionKey: "acli:system", Argv: []string{"acli", "system", "lsof"}, Node: "SIM-NODE", Container: "host"},
			Result:   fixture.ResultDef{ExitCode: 0, Stdout: "flock 9527\n"}, Fault: fixture.FaultDef{Type: fixture.FaultNone},
		}},
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	return manifest
}

func compileInput() CompileInput {
	return CompileInput{
		SupportID: "27123", KBDRevision: 24, KBDChecksum: "sha256:kbd-27123", SignalsDigest: "sha256:signals",
		ToolContractRevision: "tool-r24", PolicyRevision: "policy-r2", CompilerRevision: "git:test",
		Artifacts:    []Artifact{{ID: "artifact-1", Digest: "sha256:artifact"}},
		Dependencies: []Dependency{{Type: "tool", ID: "acli", Revision: "r24", Digest: "sha256:tool"}},
	}
}

func TestCompileInputFingerprintDoesNotMutateCaller(t *testing.T) {
	input := compileInput()
	input.Artifacts = []Artifact{{ID: "z", Digest: "sha256:z"}, {ID: "a", Digest: "sha256:a"}}
	input.Dependencies = []Dependency{
		{Type: "tool", ID: "z", Revision: "1", Digest: "sha256:z"},
		{Type: "kbd", ID: "a", Revision: "1", Digest: "sha256:a"},
	}
	if _, err := input.Fingerprint(); err != nil {
		t.Fatal(err)
	}
	if input.Artifacts[0].ID != "z" || input.Dependencies[0].Type != "tool" {
		t.Fatalf("Fingerprint 不得修改调用方顺序: %+v %+v", input.Artifacts, input.Dependencies)
	}
}

func TestCompileInputRouteSourcesAreCanonicalAndValidated(t *testing.T) {
	input := compileInput()
	input.RouteSources = []RouteSource{
		{RouteID: "route-b", SignalID: "sig-b", SourceType: "kbd_signal_contract", SourceRef: "sig-b", SourceDigest: "sha256:signals"},
		{RouteID: "route-a", SignalID: "sig-a", SourceType: "kbd_signal_contract", SourceRef: "sig-a", SourceDigest: "sha256:signals"},
	}
	first, err := input.Fingerprint()
	if err != nil {
		t.Fatal(err)
	}
	input.RouteSources[0], input.RouteSources[1] = input.RouteSources[1], input.RouteSources[0]
	second, err := input.Fingerprint()
	if err != nil || first != second {
		t.Fatalf("route source 顺序不应改变指纹: %s %s %v", first, second, err)
	}
	input.RouteSources[1].RouteID = input.RouteSources[0].RouteID
	if _, err := input.Fingerprint(); err == nil || !strings.Contains(err.Error(), "route source") {
		t.Fatalf("重复 route source 必须拒绝: %v", err)
	}
}

func registryWithApprovedArtifact(t *testing.T, now time.Time) *MemoryRegistry {
	t.Helper()
	artifacts := NewMemoryArtifactRegistry()
	metadata := ArtifactRecord{
		ID:        "artifact-1",
		Digest:    "sha256:artifact",
		SizeBytes: 1024,
		MediaType: "application/json",
		Schema:    "hci-observation/v1",
		Provenance: ArtifactProvenance{
			SourceType: "authorized-readonly-collection", SourceRefDigest: "sha256:source", RedactionDigest: "sha256:redacted",
			CollectorID: "collector", CollectedAt: now, CollectionPolicy: "policy:approved-v1",
		},
	}
	if _, err := artifacts.Register(Actor{ID: "collector", Role: RoleCompiler}, metadata, now); err != nil {
		t.Fatal(err)
	}
	if _, err := artifacts.RecordScan(Actor{ID: "scanner", Role: RoleSecurity}, metadata.ID, ArtifactScanReport{
		ScannerRevision: "scan-v1", SecretScanPassed: true, PIIScanPassed: true, LicenseScanPassed: true, SchemaValid: true,
	}, now); err != nil {
		t.Fatal(err)
	}
	if _, err := artifacts.Approve(Actor{ID: "expert", Role: RoleExpert}, metadata.ID, now); err != nil {
		t.Fatal(err)
	}
	if _, err := artifacts.Approve(Actor{ID: "security", Role: RoleSecurity}, metadata.ID, now); err != nil {
		t.Fatal(err)
	}
	return NewMemoryRegistryWithDependencies(artifacts, NewMemoryBundleObjectStore())
}

func publish(t *testing.T, registry *MemoryRegistry, now time.Time) BundleRecord {
	t.Helper()
	draft, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, compileInput(), fixtureManifest(), now)
	if err != nil {
		t.Fatal(err)
	}
	if again, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, compileInput(), fixtureManifest(), now); err != nil || again.Digest != draft.Digest {
		t.Fatalf("compile idempotency: %+v %v", again, err)
	}
	if _, err := registry.Validate(Actor{ID: "compiler", Role: RoleCompiler}, draft.Digest, ValidationReport{MutationDetected: true, SecretScanPassed: true, IndependentProof: true}, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "compiler", Role: RoleExpert}, draft.Digest, now); err == nil {
		t.Fatal("compiler self-approval must fail")
	}
	if _, err := registry.Approve(Actor{ID: "expert", Role: RoleExpert}, draft.Digest, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "security", Role: RoleSecurity}, draft.Digest, now); err != nil {
		t.Fatal(err)
	}
	published, err := registry.Publish(Actor{ID: "publisher", Role: RolePublisher}, draft.Digest, now)
	if err != nil {
		t.Fatal(err)
	}
	return published
}

func TestRegistryLifecycleIntegrityAndStale(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	published := publish(t, registry, now)
	if _, err := registry.GetPublished(published.Digest); err != nil {
		t.Fatal(err)
	}
	stale, err := registry.MarkStale(Actor{ID: "compiler", Role: RoleCompiler}, Dependency{Type: "tool", ID: "acli", Revision: "r25"}, "tool contract changed", now.Add(time.Minute))
	if err != nil || len(stale) != 1 || stale[0].Status != BundleStale {
		t.Fatalf("stale=%+v err=%v", stale, err)
	}
	if _, err := registry.GetPublished(published.Digest); err == nil {
		t.Fatal("stale bundle must not create new runs")
	}
}

func TestRegistryRejectsNonDeterministicCompilationAndSplitRoleBySameActor(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	draft, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, compileInput(), fixtureManifest(), now)
	if err != nil {
		t.Fatal(err)
	}
	changed := fixtureManifest()
	changed.Routes[0].Result.Stdout = "different\n"
	changed.Bundle.Digest = fixture.ComputeBundleDigest(changed)
	if _, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, compileInput(), changed, now); err == nil {
		t.Fatal("same immutable input with a distinct generated bundle must fail closed")
	}
	if _, err := registry.Validate(Actor{ID: "compiler", Role: RoleCompiler}, draft.Digest, ValidationReport{MutationDetected: true, SecretScanPassed: true, IndependentProof: true}, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "reviewer", Role: RoleExpert}, draft.Digest, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "reviewer", Role: RoleSecurity}, draft.Digest, now); err == nil {
		t.Fatal("one person must not satisfy both mandatory approval roles")
	}
}

func TestExpertEditCreatesImmutableDraftRevisionAndPreventsSelfApproval(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	parent, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, compileInput(), fixtureManifest(), now)
	if err != nil {
		t.Fatal(err)
	}
	editedManifest := fixtureManifest()
	editedManifest.Routes[0].Result.Stdout = "flock 9527\nexpert corrected evidence\n"
	edited, err := registry.ReviseDraft(
		Actor{ID: "expert-editor", Role: RoleExpert},
		parent.Digest,
		editedManifest,
		"修正真实采集输出中的关键进程证据",
		now.Add(time.Minute),
	)
	if err != nil {
		t.Fatal(err)
	}
	if edited.Status != BundleDraft || edited.Digest == parent.Digest || edited.Input.ParentBundleDigest != parent.Digest || edited.Input.DraftRevision != 1 {
		t.Fatalf("edited draft=%+v", edited)
	}
	if original, err := registry.Get(parent.Digest); err != nil || original.Digest != parent.Digest {
		t.Fatalf("父 Draft 必须保持可追溯: %+v %v", original, err)
	}
	if _, err := registry.Validate(Actor{ID: "compiler", Role: RoleCompiler}, edited.Digest, ValidationReport{MutationDetected: true, SecretScanPassed: true, IndependentProof: true}, now.Add(2*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "expert-editor", Role: RoleExpert}, edited.Digest, now.Add(3*time.Minute)); err == nil {
		t.Fatal("Draft 修改者不得自审同一 revision")
	}
}

func TestPublishedBundleCanBeForkedAsNewDraftWithoutMutatingActiveVersion(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	published := publish(t, registry, now)

	editedManifest := fixtureManifest()
	editedManifest.Routes[0].Result.Stdout = "flock 9527\nexpert revised published fixture\n"
	child, err := registry.ReviseDraft(
		Actor{ID: "expert-editor", Role: RoleExpert},
		published.Digest,
		editedManifest,
		"基于已发布版本补充进程输出",
		now.Add(time.Minute),
	)
	if err != nil {
		t.Fatal(err)
	}
	if child.Status != BundleDraft || child.Digest == published.Digest || child.Input.ParentBundleDigest != published.Digest || child.Input.DraftRevision != published.Input.DraftRevision+1 {
		t.Fatalf("published child=%+v", child)
	}
	parent, err := registry.Get(published.Digest)
	if err != nil || parent.Status != BundlePublished {
		t.Fatalf("已发布父版本必须保持不可变且可运行: %+v %v", parent, err)
	}
}

func TestRetireKeepsBundleReadableButHidesItFromDefaultList(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	draft, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, compileInput(), fixtureManifest(), now)
	if err != nil {
		t.Fatal(err)
	}
	retired, err := registry.Retire(Actor{ID: "expert", Role: RoleExpert}, draft.Digest, now.Add(time.Minute))
	if err != nil || retired.Status != BundleRetired {
		t.Fatalf("retire=%+v err=%v", retired, err)
	}
	if record, err := registry.Get(draft.Digest); err != nil || record.Status != BundleRetired {
		t.Fatalf("归档后仍必须可按 digest 追溯: %+v %v", record, err)
	}
	listed, err := registry.List(draft.Input.SupportID)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed) != 0 {
		t.Fatalf("归档 Bundle 不应出现在默认列表: %+v", listed)
	}
	if _, err := registry.Retire(Actor{ID: "expert", Role: RoleExpert}, draft.Digest, now.Add(2*time.Minute)); err == nil {
		t.Fatal("已归档 Bundle 不得重复归档")
	}
}

func TestRetireRejectsPublishedBundle(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	published := publish(t, registry, now)
	if _, err := registry.Retire(Actor{ID: "expert", Role: RoleExpert}, published.Digest, now.Add(time.Minute)); err == nil {
		t.Fatal("已发布 Bundle 不得通过删除入口归档")
	}
}

func TestKBDContractFlowCollectionFourScansDualReviewDraftEditAndPublish(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	artifacts := NewMemoryArtifactRegistry()
	metadata := ArtifactRecord{
		ID: "artifact-kbd-27123", Digest: "sha256:artifact-kbd-27123", SizeBytes: 2048,
		MediaType: "application/json", Schema: "hci-observation/v1", TraceID: "trace-kbd-27123-flow",
		Provenance: ArtifactProvenance{
			SourceType: "authorized-readonly-collection", SourceRefDigest: "sha256:source-kbd-27123",
			RedactionDigest: "sha256:redacted-kbd-27123", CollectorID: "collector-kbd-27123",
			CollectedAt: now, CollectionPolicy: "collection-policy-v1",
		},
	}
	registered, err := artifacts.Register(Actor{ID: "collector-service", Role: RoleCompiler}, metadata, now)
	if err != nil || registered.Status != ArtifactStaged {
		t.Fatalf("collection register=%+v err=%v", registered, err)
	}
	scanned, err := artifacts.RecordScan(Actor{ID: "scanner-service", Role: RoleSecurity}, metadata.ID, ArtifactScanReport{
		ScannerRevision: "scanner-contract-v1", SecretScanPassed: true, PIIScanPassed: true, LicenseScanPassed: true, SchemaValid: true,
	}, now.Add(time.Minute))
	if err != nil || scanned.Status != ArtifactScanned {
		t.Fatalf("four scans=%+v err=%v", scanned, err)
	}
	if _, err := artifacts.Approve(Actor{ID: "artifact-expert", Role: RoleExpert}, metadata.ID, now.Add(2*time.Minute)); err != nil {
		t.Fatal(err)
	}
	approvedArtifact, err := artifacts.Approve(Actor{ID: "artifact-security", Role: RoleSecurity}, metadata.ID, now.Add(3*time.Minute))
	if err != nil || approvedArtifact.Status != ArtifactApproved {
		t.Fatalf("artifact dual review=%+v err=%v", approvedArtifact, err)
	}

	registry := NewMemoryRegistryWithDependencies(artifacts, NewMemoryBundleObjectStore())
	input := compileInput()
	input.Artifacts = []Artifact{{ID: metadata.ID, Digest: metadata.Digest}}
	draft, err := registry.Compile(Actor{ID: "compiler-service", Role: RoleCompiler}, input, fixtureManifest(), now.Add(4*time.Minute))
	if err != nil || draft.Status != BundleDraft {
		t.Fatalf("compile draft=%+v err=%v", draft, err)
	}
	editedManifest := fixtureManifest()
	editedManifest.Routes[0].Result.Stdout = "flock 9527\nexpert verified\n"
	edited, err := registry.ReviseDraft(Actor{ID: "bundle-editor", Role: RoleExpert}, draft.Digest, editedManifest, "补充专家确认的进程证据", now.Add(5*time.Minute))
	if err != nil || edited.Status != BundleDraft {
		t.Fatalf("expert draft=%+v err=%v", edited, err)
	}
	if _, err := registry.Validate(Actor{ID: "compiler-service", Role: RoleCompiler}, edited.Digest, ValidationReport{MutationDetected: true, SecretScanPassed: true, IndependentProof: true}, now.Add(6*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "bundle-expert", Role: RoleExpert}, edited.Digest, now.Add(7*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "bundle-security", Role: RoleSecurity}, edited.Digest, now.Add(8*time.Minute)); err != nil {
		t.Fatal(err)
	}
	published, err := registry.Publish(Actor{ID: "publisher-service", Role: RolePublisher}, edited.Digest, now.Add(9*time.Minute))
	if err != nil || published.Status != BundlePublished {
		t.Fatalf("publish=%+v err=%v", published, err)
	}
	if _, err := registry.GetPublished(published.Digest); err != nil {
		t.Fatalf("published bundle integrity: %v", err)
	}
}

type passingRunner struct{ seen TestRun }

func (r *passingRunner) Run(_ context.Context, run TestRun, token string) (RunStatus, error) {
	if token == "" {
		return RunInconclusive, nil
	}
	r.seen = run
	return RunPassed, nil
}

func TestRunIsPinnedIdempotentAndCapacityBounded(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := registryWithApprovedArtifact(t, now)
	bundle := publish(t, registry, now)
	store, err := NewRunStore(registry, []byte("0123456789abcdef0123456789abcdef"), "hci-platform", "hci-sim", 1)
	if err != nil {
		t.Fatal(err)
	}
	request := CreateRunRequest{IDempotencyKey: "ci-27123-1", SupportID: "27123", Variant: "positive-realistic", Node: "SIM-NODE", Container: "host", Deadline: now.Add(time.Hour)}
	run, err := store.Create(request, now)
	if err != nil {
		t.Fatal(err)
	}
	if duplicate, err := store.Create(request, now); err != nil || duplicate.ID != run.ID || duplicate.BundleDigest != bundle.Digest {
		t.Fatalf("idempotency failed: %+v %v", duplicate, err)
	}
	runner := &passingRunner{}
	completed, err := store.Start(context.Background(), run.ID, runner, now)
	if err != nil || completed.Status != RunPassed || runner.seen.BundleDigest != bundle.Digest || runner.seen.LeaseJTI == "" {
		t.Fatalf("run=%+v runner=%+v err=%v", completed, runner.seen, err)
	}
	if _, err := store.CreateForBundle(CreateRunRequest{IDempotencyKey: "ci-27123-1", SupportID: "27123", Variant: "negative", Node: "SIM-NODE", Container: "host", Deadline: now.Add(time.Hour)}, bundle.Digest, now); err == nil {
		t.Fatal("same idempotency key with different request must conflict")
	}
	if _, err := store.CreateForBundle(CreateRunRequest{IDempotencyKey: "ci-27123-2", SupportID: "27123", Variant: "negative", Node: "SIM-NODE", Container: "host", Deadline: now.Add(time.Hour)}, bundle.Digest, now); err == nil {
		t.Fatal("a run must not pin a bundle that lacks the requested variant/target route")
	}
}

func TestDifferentialAndStabilityDoNotHideSemanticDifferences(t *testing.T) {
	real := Observation{CommandFingerprint: "sha256:command", Stdout: "ok at 10:00", ExitCode: 0, Outcome: RunPassed}
	sim := real
	sim.Stdout = "ok at 10:01"
	normalizeTimestamp := func(value string) string { return strings.Split(value, " at ")[0] }
	if result := CompareObservations(real, sim, normalizeTimestamp); !result.Equal {
		t.Fatalf("approved timestamp normalization should match: %+v", result)
	}
	sim.ExitCode = 1
	if result := CompareObservations(real, sim, normalizeTimestamp); result.Equal || len(result.Differences) != 1 || result.Differences[0] != "exit_code" {
		t.Fatalf("exit difference hidden: %+v", result)
	}
	report := SummarizeStability([]RunStatus{RunPassed, RunPassed, RunInconclusive}, []time.Duration{time.Second, 3 * time.Second, 2 * time.Second})
	if report.Runs != 3 || report.FirstAttemptPasses != 2 || report.P50 != 2*time.Second {
		t.Fatalf("stability=%+v", report)
	}
}

func TestMutationAndCapacityGatesFailClosed(t *testing.T) {
	classify := func(observation Observation) string {
		if observation.ExitCode != 0 || observation.Outcome != RunPassed {
			return "error"
		}
		return observation.Stdout
	}
	report := AssessMutations([]MutationCase{
		{ID: "keyword-delete", Original: Observation{Stdout: "locked", Outcome: RunPassed}, Mutant: Observation{Stdout: "clear", Outcome: RunPassed}, Critical: true},
		{ID: "ignored-output", Original: Observation{Stdout: "same", Outcome: RunPassed}, Mutant: Observation{Stdout: "same", Outcome: RunPassed}, Critical: true},
		{ID: "expert-equivalent", Equivalent: true},
	}, classify)
	if report.ValidMutants != 2 || report.DetectedMutants != 1 || len(report.CriticalMisses) != 1 || report.CriticalMisses[0] != "ignored-output" {
		t.Fatalf("mutation report=%+v", report)
	}
	if err := ValidateCapacityLadder([]CapacityEvidence{{Concurrent: 1, Completed: 1}, {Concurrent: 10, Completed: 10}, {Concurrent: 50, Completed: 50}, {Concurrent: 100, Completed: 100}, {Concurrent: 200, Completed: 195, ExplicitOverload: true}}); err != nil {
		t.Fatal(err)
	}
	if err := ValidateCapacityLadder([]CapacityEvidence{{Concurrent: 1, Completed: 1}, {Concurrent: 10, Completed: 10, CrossScenarioContamination: 1}}); err == nil {
		t.Fatal("cross-scenario contamination must be a P0 stop condition")
	}
}
