package database

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/fixture"
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
	suffix := fmt.Sprintf("%08x", uint32(time.Now().UnixNano()&0xffffffff))
	now := time.Now().UTC()
	record := controlplane.ArtifactRecord{
		ID:        "art-" + suffix,
		Digest:    integrationDigest("artifact-" + suffix),
		SizeBytes: 1024,
		MediaType: "application/json",
		Schema:    "v1",
		Provenance: controlplane.ArtifactProvenance{
			SourceType:       "controlled_collect",
			SourceRefDigest:  integrationDigest("source-" + suffix),
			RedactionDigest:  integrationDigest("redact-" + suffix),
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
	// support_id 限制为 20 字符，使用 8 位 hex suffix
	suffix := fmt.Sprintf("%08x", uint32(time.Now().UnixNano()&0xffffffff))
	now := time.Now().UTC()
	input := controlplane.CompileInput{
		SupportID:            "t" + suffix,
		KBDRevision:          1,
		KBDChecksum:          integrationDigest("kbd-checksum-" + suffix),
		SignalsDigest:        integrationDigest("signals-" + suffix),
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
