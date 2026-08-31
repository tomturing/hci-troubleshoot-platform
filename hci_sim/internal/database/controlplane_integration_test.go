package database

import (
	"context"
	"os"
	"testing"
	"time"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/fixture"

	"github.com/google/uuid"
)

func TestArtifactRepositoryFullLifecycle(t *testing.T) {
	rawURL := os.Getenv("HCI_SIM_TEST_DATABASE_URL")
	if rawURL == "" {
		t.Skip("HCI_SIM_TEST_DATABASE_URL is not configured")
	}
	target, err := Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := Open(ctx, target)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	artifactRepo, err := NewArtifactRepository(pool)
	if err != nil {
		t.Fatal(err)
	}
	// 使用 UUID 确保唯一性，避免并发测试冲突
	testID := uuid.New().String()[:8]
	now := time.Now().UTC()
	record := controlplane.ArtifactRecord{
		ID:        "art-" + testID,
		Digest:    integrationDigest("artifact-" + testID),
		SizeBytes: 1024,
		MediaType: "application/json",
		Schema:    "v1",
		Provenance: controlplane.ArtifactProvenance{
			SourceType:       "controlled_collect",
			SourceRefDigest:  integrationDigest("source-" + testID),
			RedactionDigest:  integrationDigest("redact-" + testID),
			CollectorID:      "collector-test",
			CollectedAt:      now,
			CollectionPolicy: "test-policy",
		},
	}
	registered, err := artifactRepo.Register(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleCompiler}, record, now)
	if err != nil {
		t.Fatalf("register artifact: %v", err)
	}
	if registered.Status != controlplane.ArtifactStaged {
		t.Fatalf("expected staged, got %s", registered.Status)
	}
	scan := controlplane.ArtifactScanReport{
		ScannerRevision:   "scanner-v1",
		SecretScanPassed:  true,
		PIIScanPassed:     true,
		LicenseScanPassed: true,
		SchemaValid:       true,
	}
	scanned, err := artifactRepo.RecordScan(controlplane.Actor{ID: "security-test", Role: controlplane.RoleSecurity}, record.ID, scan, now)
	if err != nil {
		t.Fatalf("record scan: %v", err)
	}
	if scanned.Status != controlplane.ArtifactScanned {
		t.Fatalf("expected scanned, got %s", scanned.Status)
	}
	_, err = artifactRepo.Approve(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleExpert}, record.ID, now)
	if err == nil {
		t.Fatal("expected self-approval rejection")
	}
	approved1, err := artifactRepo.Approve(controlplane.Actor{ID: "expert-test", Role: controlplane.RoleExpert}, record.ID, now)
	if err != nil {
		t.Fatalf("expert approve: %v", err)
	}
	if approved1.Status != controlplane.ArtifactScanned {
		t.Fatalf("expected still scanned before security approval, got %s", approved1.Status)
	}
	approved2, err := artifactRepo.Approve(controlplane.Actor{ID: "security-test", Role: controlplane.RoleSecurity}, record.ID, now)
	if err != nil {
		t.Fatalf("security approve: %v", err)
	}
	if approved2.Status != controlplane.ArtifactApproved {
		t.Fatalf("expected approved after dual approval, got %s", approved2.Status)
	}
	if err := artifactRepo.VerifyApproved(controlplane.Artifact{ID: record.ID, Digest: record.Digest}); err != nil {
		t.Fatalf("verify approved: %v", err)
	}
}

func TestBundleRegistryCompileAndLifecycle(t *testing.T) {
	rawURL := os.Getenv("HCI_SIM_TEST_DATABASE_URL")
	if rawURL == "" {
		t.Skip("HCI_SIM_TEST_DATABASE_URL is not configured")
	}
	target, err := Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := Open(ctx, target)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	artifactRepo, _ := NewArtifactRepository(pool)
	bundleRepo, err := NewBundleRegistry(pool, artifactRepo, controlplane.NewMemoryBundleObjectStore())
	if err != nil {
		t.Fatal(err)
	}
	// support_id 限制为 20 字符，使用 8 位 UUID prefix
	testID := uuid.New().String()[:8]
	now := time.Now().UTC()
	input := controlplane.CompileInput{
		SupportID:            "t" + testID,
		KBDRevision:          1,
		KBDChecksum:          integrationDigest("kbd-" + testID),
		SignalsDigest:        integrationDigest("signals-" + testID),
		ToolContractRevision: "tool-r1",
		PolicyRevision:       "policy-r1",
		CompilerRevision:     "compiler-v1",
	}
	manifest := fixture.Manifest{
		SchemaVersion: "2.0",
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: input.SupportID, Revision: input.KBDRevision, Checksum: input.KBDChecksum},
		Contracts:     fixture.Contracts{ToolRevision: input.ToolContractRevision, PolicyRevision: input.PolicyRevision},
		Variables:     map[string]string{"SYNTHETIC": "true"},
		Limits:        fixture.Limits{MaxRoutes: 1, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes: []fixture.Route{
			{ID: "synthetic-route", SignalID: "sig-1", Variant: "positive-minimal",
				RouteKey: fixture.RouteKey{Tool: "acli", AcquisitionKey: "acli:test", Argv: []string{"acli", "test"}, Node: "node-1", Container: "host"},
				Result:   fixture.ResultDef{ExitCode: 0, Stdout: "{}\n"}},
		},
	}
	drafted, err := bundleRepo.Compile(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleCompiler}, input, manifest, now)
	if err != nil {
		t.Fatalf("compile draft: %v", err)
	}
	if drafted.Status != controlplane.BundleDraft {
		t.Fatalf("expected draft, got %s", drafted.Status)
	}
	if len(drafted.Input.RouteSources) != len(manifest.Routes) || drafted.Input.RouteSources[0].SourceRef != manifest.Routes[0].SignalID {
		t.Fatalf("compile_input 必须冻结 route_sources: %+v", drafted.Input.RouteSources)
	}
	retireInput := input
	retireInput.KBDRevision = 2
	retireInput.KBDChecksum = integrationDigest("kbd-retire-" + testID)
	retireManifest := manifest
	retireManifest.KBD.Revision = retireInput.KBDRevision
	retireManifest.KBD.Checksum = retireInput.KBDChecksum
	retireCandidate, err := bundleRepo.Compile(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleCompiler}, retireInput, retireManifest, now)
	if err != nil {
		t.Fatalf("compile retire candidate: %v", err)
	}
	retired, err := bundleRepo.Retire(controlplane.Actor{ID: "expert-test", Role: controlplane.RoleExpert}, retireCandidate.Digest, now)
	if err != nil || retired.Status != controlplane.BundleRetired {
		t.Fatalf("retire draft: record=%+v err=%v", retired, err)
	}
	if gotRetired, err := bundleRepo.Get(retireCandidate.Digest); err != nil || gotRetired.Status != controlplane.BundleRetired {
		t.Fatalf("retired bundle must remain readable: record=%+v err=%v", gotRetired, err)
	}
	listed, err := bundleRepo.List(input.SupportID)
	if err != nil || len(listed) != 1 || listed[0].Digest != drafted.Digest {
		t.Fatalf("retired bundle must be hidden from default list: bundles=%+v err=%v", listed, err)
	}
	validated, err := bundleRepo.Validate(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleCompiler}, drafted.Digest, controlplane.ValidationReport{MutationDetected: true, SecretScanPassed: true, IndependentProof: true}, now)
	if err != nil {
		t.Fatalf("validate: %v", err)
	}
	if validated.Status != controlplane.BundleValidated {
		t.Fatalf("expected validated, got %s", validated.Status)
	}
	_, err = bundleRepo.Approve(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleExpert}, drafted.Digest, now)
	if err == nil {
		t.Fatal("expected self-approval rejection")
	}
	approved1, err := bundleRepo.Approve(controlplane.Actor{ID: "expert-test", Role: controlplane.RoleExpert}, drafted.Digest, now)
	if err != nil {
		t.Fatalf("expert approve: %v", err)
	}
	if approved1.Status != controlplane.BundleValidated {
		t.Fatalf("expected still validated before security, got %s", approved1.Status)
	}
	if _, err := bundleRepo.Approve(controlplane.Actor{ID: "expert-test", Role: controlplane.RoleSecurity}, drafted.Digest, now); err == nil {
		t.Fatal("same actor must not satisfy expert and security approval")
	}
	approved2, err := bundleRepo.Approve(controlplane.Actor{ID: "security-test", Role: controlplane.RoleSecurity}, drafted.Digest, now)
	if err != nil {
		t.Fatalf("security approve: %v", err)
	}
	if approved2.Status != controlplane.BundleApproved {
		t.Fatalf("expected approved, got %s", approved2.Status)
	}
	published, err := bundleRepo.Publish(controlplane.Actor{ID: "publisher-test", Role: controlplane.RolePublisher}, drafted.Digest, now)
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	if published.Status != controlplane.BundlePublished {
		t.Fatalf("expected published, got %s", published.Status)
	}
	got, err := bundleRepo.GetPublished(drafted.Digest)
	if err != nil {
		t.Fatalf("get published: %v", err)
	}
	if got.Digest != drafted.Digest {
		t.Fatalf("digest mismatch")
	}
	resolved, err := bundleRepo.ResolvePublished(input.SupportID, "positive-minimal", "node-1", "host")
	if err != nil {
		t.Fatalf("resolve published: %v", err)
	}
	if resolved.Digest != drafted.Digest {
		t.Fatalf("resolved digest mismatch")
	}
	stale, err := bundleRepo.MarkStale(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleCompiler}, controlplane.Dependency{Type: "tool", ID: "test-tool", Revision: "v2", Digest: "sha256:new"}, "dependency_changed", now)
	if err != nil {
		t.Fatalf("mark stale: %v", err)
	}
	if len(stale) != 0 {
		// Bundle did not depend on the changed tool, so no stale records expected
		t.Fatalf("unexpected stale bundles: %d", len(stale))
	}
}

func TestBundleRegistryReviseDraftCAS(t *testing.T) {
	rawURL := os.Getenv("HCI_SIM_TEST_DATABASE_URL")
	if rawURL == "" {
		t.Skip("HCI_SIM_TEST_DATABASE_URL is not configured")
	}
	target, err := Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := Open(ctx, target)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	bundleRepo, err := NewBundleRegistry(pool, nil, controlplane.NewMemoryBundleObjectStore())
	if err != nil {
		t.Fatal(err)
	}
	testID := uuid.New().String()[:8]
	now := time.Now().UTC()
	input := controlplane.CompileInput{
		SupportID:            "r" + testID,
		KBDRevision:          1,
		KBDChecksum:          integrationDigest("kbd-revise-" + testID),
		SignalsDigest:        integrationDigest("signals-revise-" + testID),
		ToolContractRevision: "tool-r1",
		PolicyRevision:       "policy-r1",
		CompilerRevision:     "compiler-v1",
	}
	manifest := fixture.Manifest{
		SchemaVersion: "2.0",
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: input.SupportID, Revision: input.KBDRevision, Checksum: input.KBDChecksum},
		Contracts:     fixture.Contracts{ToolRevision: input.ToolContractRevision, PolicyRevision: input.PolicyRevision},
		Variables:     map[string]string{"SYNTHETIC": "true"},
		Limits:        fixture.Limits{MaxRoutes: 1, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes: []fixture.Route{{
			ID: "synthetic-route", SignalID: "sig-1", Variant: "positive-minimal",
			RouteKey: fixture.RouteKey{Tool: "acli", AcquisitionKey: "acli:test", Argv: []string{"acli", "test"}, Node: "node-1", Container: "host"},
			Result:   fixture.ResultDef{ExitCode: 0, Stdout: "before\n"},
		}},
	}
	parent, err := bundleRepo.Compile(controlplane.Actor{ID: "compiler-test", Role: controlplane.RoleCompiler}, input, manifest, now)
	if err != nil {
		t.Fatalf("compile parent draft: %v", err)
	}
	edited := manifest
	edited.Routes = append([]fixture.Route(nil), manifest.Routes...)
	edited.Routes[0].Result.Stdout = "after\n"
	child, err := bundleRepo.ReviseDraft(
		controlplane.Actor{ID: "expert-test", Role: controlplane.RoleExpert},
		parent.Digest,
		edited,
		"更新验证输出",
		now.Add(time.Second),
	)
	if err != nil {
		t.Fatalf("revise draft: %v", err)
	}
	if child.Input.ParentBundleDigest != parent.Digest {
		t.Fatalf("child parent digest = %q, want %q", child.Input.ParentBundleDigest, parent.Digest)
	}
	parentAfter, err := bundleRepo.Get(parent.Digest)
	if err != nil || parentAfter.Status != controlplane.BundleStale {
		t.Fatalf("parent must become stale atomically: record=%+v err=%v", parentAfter, err)
	}
	if _, err := bundleRepo.ReviseDraft(
		controlplane.Actor{ID: "expert-test-2", Role: controlplane.RoleExpert},
		parent.Digest,
		manifest,
		"基于过期父版本再次修订",
		now.Add(2*time.Second),
	); err == nil {
		t.Fatal("stale parent revision must be rejected")
	}
	var draftCount int
	if err := pool.QueryRow(ctx, `
		SELECT count(*)
		FROM fixture.bundle b
		JOIN control_plane.scenario s ON s.id = b.scenario_id
		WHERE s.support_id = $1 AND b.status = 'draft'
	`, input.SupportID).Scan(&draftCount); err != nil || draftCount != 1 {
		t.Fatalf("active draft count=%d, want 1: %v", draftCount, err)
	}
}
