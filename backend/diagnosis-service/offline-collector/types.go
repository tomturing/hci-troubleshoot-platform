package main

import "encoding/json"

const (
	exitInternal   = 1
	exitUsage      = 2
	exitPreflight  = 3
	exitVerify     = 4
	exitDeclined   = 5
	exitCollection = 6
	exitPackaging  = 7
	magicV1        = "HCIEB1\n"
	magicV2        = "HCIEB2\n"
	magic          = magicV1
)

type documentSignature struct {
	Algorithm            string `json:"algorithm"`
	KeyID                string `json:"key_id"`
	SignatureBase64      string `json:"signature_base64"`
	PublicKeyFingerprint string `json:"public_key_fingerprint"`
}

type artifactSignature struct {
	Algorithm            string `json:"algorithm"`
	KeyID                string `json:"key_id"`
	SignatureBase64      string `json:"signature_base64"`
	PublicKeyBase64      string `json:"public_key_base64"`
	PublicKeyFingerprint string `json:"public_key_fingerprint"`
	SignedAt             string `json:"signed_at"`
	ExpiresAt            string `json:"expires_at"`
}

type timeWindow struct {
	StartTime string `json:"start_time"`
	EndTime   string `json:"end_time"`
}

type outputContract struct {
	OutputPath string `json:"output_path"`
	MediaType  string `json:"media_type"`
}

type artifactItem struct {
	PlanItemID        string          `json:"plan_item_id"`
	CollectorID       string          `json:"collector_id"`
	CollectorRevision int             `json:"collector_revision"`
	CollectorChecksum string          `json:"collector_checksum"`
	Executor          string          `json:"executor"`
	Target            json.RawMessage `json:"target"`
	TimeWindow        timeWindow      `json:"time_window"`
	OutputContract    outputContract  `json:"output_contract"`
}

type artifactProfile struct {
	Name     string `json:"name"`
	Revision int    `json:"revision"`
	Version  string `json:"version"`
	Checksum string `json:"checksum"`
}

type bundleEncryption struct {
	Algorithm          string `json:"algorithm"`
	KeyWrapAlgorithm   string `json:"key_wrap_algorithm"`
	KeyID              string `json:"key_id"`
	PublicKeyPEMBase64 string `json:"public_key_pem_base64"`
	Format             string `json:"format"`
}

type artifactManifest struct {
	SchemaVersion    string            `json:"schema_version"`
	ArtifactType     string            `json:"artifact_type"`
	ArtifactID       string            `json:"artifact_id"`
	SessionID        string            `json:"session_id"`
	CollectionPlanID string            `json:"collection_plan_id"`
	TargetKey        string            `json:"target_key"`
	FileName         string            `json:"file_name"`
	Profile          artifactProfile   `json:"profile"`
	BundleEncryption *bundleEncryption `json:"bundle_encryption"`
	ArtifactSHA256   string            `json:"artifact_sha256"`
	Signature        artifactSignature `json:"signature"`
	CollectionItems  []artifactItem    `json:"collection_items"`
}

type collectorArtifact struct {
	SchemaVersion    string                   `json:"schema_version"`
	ArtifactID       string                   `json:"artifact_id"`
	SessionID        string                   `json:"session_id"`
	CollectionPlanID string                   `json:"collection_plan_id"`
	TargetKey        string                   `json:"target_key"`
	ExecutionItems   []collectorExecutionItem `json:"execution_items"`
}

type collectorExecutionItem struct {
	ItemID            string            `json:"item_id"`
	CollectorID       string            `json:"collector_id"`
	SourceSignalRefs  []sourceSignalRef `json:"source_signal_refs,omitempty"`
	CollectorRevision int               `json:"collector_revision"`
	CollectorChecksum string            `json:"collector_checksum"`
	Executor          string            `json:"executor"`
	TimeoutSeconds    int               `json:"timeout_seconds"`
	MaxOutputBytes    int64             `json:"max_output_bytes"`
	Argv              []string          `json:"argv,omitempty"`
	Method            string            `json:"method,omitempty"`
	Path              string            `json:"path,omitempty"`
	Guide             string            `json:"guide,omitempty"`
}

// sourceSignalRef is signed provenance metadata. The collector does not use it
// to choose commands; it is preserved for deterministic diagnosis-lab binding
// and cross-system audit without inferring business identity from argv text.
type sourceSignalRef struct {
	SupportID string `json:"support_id"`
	SignalID  string `json:"signal_id"`
}

type caseDocument struct {
	CaseID           string            `json:"case_id"`
	SessionID        string            `json:"session_id"`
	SelectedScenario string            `json:"selected_scenario"`
	IncidentWindow   map[string]string `json:"incident_window"`
	IncidentTimezone string            `json:"incident_timezone"`
	Targets          []map[string]any  `json:"targets"`
}

type executionRow struct {
	ItemID          string `json:"item_id"`
	CollectorID     string `json:"collector_id"`
	ExitCode        *int   `json:"exit_code"`
	Status          string `json:"status"`
	StdoutBytes     int64  `json:"stdout_bytes,omitempty"`
	StderrBytes     int64  `json:"stderr_bytes,omitempty"`
	OutputTruncated bool   `json:"output_truncated"`
}

type evidenceFile struct {
	Path         string `json:"path"`
	OriginalName string `json:"original_name"`
	MediaType    string `json:"media_type"`
	Sensitivity  string `json:"sensitivity"`
	SizeBytes    int64  `json:"size_bytes"`
	SHA256       string `json:"sha256"`
}

type evidenceTimeCoverage struct {
	Start string `json:"start"`
	End   string `json:"end"`
}

type evidenceCollectionItem struct {
	CollectorID    string                `json:"collector_id"`
	Status         string                `json:"status"`
	Source         string                `json:"source"`
	SourceTimezone string                `json:"source_timezone"`
	ClockOffsetMS  int                   `json:"clock_offset_ms"`
	TimeCoverage   *evidenceTimeCoverage `json:"time_coverage,omitempty"`
	Files          []evidenceFile        `json:"files"`
	ExitCode       *int                  `json:"exit_code"`
	FailureReason  *string               `json:"failure_reason"`
}

type evidenceManifest struct {
	SchemaVersion            string                   `json:"schema_version"`
	BundleID                 string                   `json:"bundle_id"`
	CaseID                   string                   `json:"case_id"`
	SessionID                string                   `json:"session_id"`
	BundleType               string                   `json:"bundle_type"`
	ParentBundleID           *string                  `json:"parent_bundle_id"`
	SelectedScenario         string                   `json:"selected_scenario"`
	CollectionProfileVersion string                   `json:"collection_profile_version"`
	CollectionPlanID         string                   `json:"collection_plan_id"`
	CollectorArtifactVersion string                   `json:"collector_artifact_version"`
	CollectorArtifactSHA256  string                   `json:"collector_artifact_sha256"`
	SignatureKeyID           string                   `json:"signature_key_id"`
	GeneratedAt              string                   `json:"generated_at"`
	IncidentWindow           map[string]string        `json:"incident_window"`
	Targets                  []map[string]any         `json:"targets"`
	CollectionItems          []evidenceCollectionItem `json:"collection_items"`
	Encryption               map[string]string        `json:"encryption"`
}

type collectionStats struct {
	Success int
	Failed  int
	Manual  int
}
