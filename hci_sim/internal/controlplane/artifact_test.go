package controlplane

import (
	"strings"
	"testing"
	"time"
)

func artifactMetadata(now time.Time) ArtifactRecord {
	return ArtifactRecord{
		ID:        "artifact-1",
		Digest:    "sha256:artifact",
		SizeBytes: 512,
		MediaType: "application/json",
		Schema:    "hci-observation/v1",
		Provenance: ArtifactProvenance{
			SourceType: "authorized-readonly-collection", SourceRefDigest: "sha256:source", RedactionDigest: "sha256:redacted",
			CollectorID: "collector", CollectedAt: now, CollectionPolicy: "policy:approved-v1",
		},
	}
}

func approvedArtifactRegistry(t *testing.T, now time.Time) *MemoryArtifactRegistry {
	t.Helper()
	registry := NewMemoryArtifactRegistry()
	artifact := artifactMetadata(now)
	if _, err := registry.Register(Actor{ID: "collector", Role: RoleCompiler}, artifact, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.RecordScan(Actor{ID: "scanner", Role: RoleSecurity}, artifact.ID, ArtifactScanReport{
		ScannerRevision: "scanner-v1", SecretScanPassed: true, PIIScanPassed: true, LicenseScanPassed: true, SchemaValid: true,
	}, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "expert", Role: RoleExpert}, artifact.ID, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "security", Role: RoleSecurity}, artifact.ID, now); err != nil {
		t.Fatal(err)
	}
	return registry
}

func TestArtifactLifecycleRequiresScanSeparatedApprovalAndRevocation(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	registry := NewMemoryArtifactRegistry()
	artifact := artifactMetadata(now)
	if _, err := registry.Register(Actor{ID: "collector", Role: RoleCompiler}, artifact, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "expert", Role: RoleExpert}, artifact.ID, now); err == nil {
		t.Fatal("unscanned Artifact must not be approvable")
	}
	if _, err := registry.RecordScan(Actor{ID: "scanner", Role: RoleSecurity}, artifact.ID, ArtifactScanReport{
		ScannerRevision: "scanner-v1", SecretScanPassed: true, PIIScanPassed: true, LicenseScanPassed: false, SchemaValid: true,
	}, now); err == nil {
		t.Fatal("failed license scan must fail closed")
	}
	if _, err := registry.RecordScan(Actor{ID: "scanner", Role: RoleSecurity}, artifact.ID, ArtifactScanReport{
		ScannerRevision: "scanner-v1", SecretScanPassed: true, PIIScanPassed: true, LicenseScanPassed: true, SchemaValid: true,
	}, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "expert", Role: RoleExpert}, artifact.ID, now); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(Actor{ID: "expert", Role: RoleSecurity}, artifact.ID, now); err == nil {
		t.Fatal("one person must not satisfy expert and security Artifact approval")
	}
	approved, err := registry.Approve(Actor{ID: "security", Role: RoleSecurity}, artifact.ID, now)
	if err != nil || approved.Status != ArtifactApproved {
		t.Fatalf("approved=%+v err=%v", approved, err)
	}
	if err := registry.VerifyApproved(Artifact{ID: artifact.ID, Digest: artifact.Digest}); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Revoke(Actor{ID: "security", Role: RoleSecurity}, artifact.ID, "retention policy revoked", now); err != nil {
		t.Fatal(err)
	}
	if err := registry.VerifyApproved(Artifact{ID: artifact.ID, Digest: artifact.Digest}); err == nil {
		t.Fatal("revoked Artifact must be rejected by Compiler gate")
	}
}

func TestCompilerRejectsCallerClaimAndAcceptsOnlyRegistryApprovedArtifact(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	input := compileInput()
	if _, err := NewMemoryRegistry().Compile(Actor{ID: "compiler", Role: RoleCompiler}, input, fixtureManifest(), now); err == nil || !strings.Contains(err.Error(), "审批 Registry 未配置") {
		t.Fatalf("caller-provided artifact must not bypass registry: %v", err)
	}
	gate := approvedArtifactRegistry(t, now)
	registry := NewMemoryRegistryWithDependencies(gate, NewMemoryBundleObjectStore())
	if _, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, input, fixtureManifest(), now); err != nil {
		t.Fatal(err)
	}
	wrongDigest := input
	wrongDigest.Artifacts = []Artifact{{ID: "artifact-1", Digest: "sha256:another"}}
	if _, err := registry.Compile(Actor{ID: "compiler", Role: RoleCompiler}, wrongDigest, fixtureManifest(), now); err == nil || !strings.Contains(err.Error(), "artifact_not_approved") {
		t.Fatalf("mismatched artifact digest must fail closed: %v", err)
	}
}

func TestBundleObjectStoreCommitsOnlyVerifiedContentAddressedBytes(t *testing.T) {
	store := NewMemoryBundleObjectStore()
	raw := []byte("immutable bundle")
	if _, err := store.Prepare(raw, "sha256:wrong"); err == nil {
		t.Fatal("prepare must reject mismatched digest")
	}
	digest := digestBytes(raw)
	staged, err := store.Prepare(raw, digest)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Verify(staged); err != nil {
		t.Fatal(err)
	}
	published, err := store.Commit(staged)
	if err != nil || published.Key != "bundles/"+digest || published.Digest != digest {
		t.Fatalf("published=%+v err=%v", published, err)
	}
	if err := store.Verify(staged); err == nil {
		t.Fatal("committed temporary reference must no longer verify as staged")
	}
	if loaded, err := store.ReadPublished(published); err != nil || string(loaded) != string(raw) {
		t.Fatalf("published payload must be readable and integrity-checked: %q %v", loaded, err)
	}
}
