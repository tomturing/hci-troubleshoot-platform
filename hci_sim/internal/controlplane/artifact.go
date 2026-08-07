package controlplane

// Artifact 与 Bundle 对象存储的控制面契约。
//
// 原始 Artifact 从不进入 PostgreSQL metadata 或 Fixture Manifest。控制面只保存其摘要、
// 参数化/脱敏摘要、采集来源摘要及审批记录；真实字节只能由受控对象存储持有。

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"sync"
	"time"
)

type ArtifactStatus string

const (
	ArtifactStaged   ArtifactStatus = "staged"
	ArtifactScanned  ArtifactStatus = "scanned"
	ArtifactApproved ArtifactStatus = "approved"
	ArtifactRevoked  ArtifactStatus = "revoked"
)

// ArtifactProvenance 只接受不可逆的来源引用，禁止客户 URL、命令输出或身份正文进入控制面。
type ArtifactProvenance struct {
	SourceType       string
	SourceRefDigest  string
	RedactionDigest  string
	CollectorID      string
	CollectedAt      time.Time
	CollectionPolicy string
}

// ArtifactScanReport 是批准之前必须有的机器证据。扫描器版本进入审计，不能只传一个 bool。
type ArtifactScanReport struct {
	ScannerRevision   string
	SecretScanPassed  bool
	PIIScanPassed     bool
	LicenseScanPassed bool
	SchemaValid       bool
	ScannedAt         time.Time
}

type ArtifactApproval struct {
	ActorID string
	Role    Role
	At      time.Time
}

// ArtifactRecord 是可绑定到编译输入的元数据，不承载真实 Artifact 内容或任意对象 URL。
type ArtifactRecord struct {
	ID          string
	Digest      string
	SizeBytes   int64
	MediaType   string
	Schema      string
	Provenance  ArtifactProvenance
	Status      ArtifactStatus
	IngestedBy  string
	TraceID     string
	Scan        *ArtifactScanReport
	Approvals   []ArtifactApproval
	CreatedAt   time.Time
	UpdatedAt   time.Time
	RevokeCause string
}

func (record ArtifactRecord) clone() ArtifactRecord {
	record.Approvals = append([]ArtifactApproval(nil), record.Approvals...)
	if record.Scan != nil {
		copy := *record.Scan
		record.Scan = &copy
	}
	return record
}

// ArtifactGate 是 Compiler 的唯一 Artifact 判断入口。生产实现必须由 PostgreSQL 的 immutable
// metadata、审批和撤销状态支持，不能信任编译请求中的 "approved" 字段。
type ArtifactGate interface {
	VerifyApproved(Artifact) error
}

// ArtifactRegistry 把采集、扫描、双人审批和撤销显式建模；同一人不得完成两个强制审批角色。
type ArtifactRegistry interface {
	ArtifactGate
	Register(Actor, ArtifactRecord, time.Time) (ArtifactRecord, error)
	RecordScan(Actor, string, ArtifactScanReport, time.Time) (ArtifactRecord, error)
	Approve(Actor, string, time.Time) (ArtifactRecord, error)
	Revoke(Actor, string, string, time.Time) (ArtifactRecord, error)
	Get(string) (ArtifactRecord, error)
}

// MemoryArtifactRegistry 仅用于确定性单元测试。它刻意没有原始字节字段，防止测试替身变成
// 绕过对象存储的第二条 Artifact 数据通道。
type MemoryArtifactRegistry struct {
	mu      sync.Mutex
	records map[string]ArtifactRecord
}

func NewMemoryArtifactRegistry() *MemoryArtifactRegistry {
	return &MemoryArtifactRegistry{records: map[string]ArtifactRecord{}}
}

func (r *MemoryArtifactRegistry) Register(actor Actor, record ArtifactRecord, now time.Time) (ArtifactRecord, error) {
	if actor.ID == "" || actor.Role != RoleCompiler {
		return ArtifactRecord{}, errors.New("forbidden: 仅受信任 compiler 可登记 Artifact metadata")
	}
	if err := validateArtifactMetadata(record); err != nil {
		return ArtifactRecord{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, ok := r.records[record.ID]; ok {
		if existing.Digest != record.Digest || existing.Provenance != record.Provenance {
			return ArtifactRecord{}, errors.New("artifact_id_conflict")
		}
		return existing.clone(), nil
	}
	record.Status = ArtifactStaged
	record.IngestedBy = actor.ID
	record.CreatedAt, record.UpdatedAt = now.UTC(), now.UTC()
	r.records[record.ID] = record
	return record.clone(), nil
}

func (r *MemoryArtifactRegistry) RecordScan(actor Actor, id string, scan ArtifactScanReport, now time.Time) (ArtifactRecord, error) {
	if actor.ID == "" || actor.Role != RoleSecurity {
		return ArtifactRecord{}, errors.New("forbidden: 仅 security 可记录 Artifact 扫描")
	}
	if scan.ScannerRevision == "" || !scan.SecretScanPassed || !scan.PIIScanPassed || !scan.LicenseScanPassed || !scan.SchemaValid {
		return ArtifactRecord{}, errors.New("artifact_scan_failed: secret、PII、license 与 schema 扫描必须全部通过")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[id]
	if !ok {
		return ArtifactRecord{}, errors.New("artifact_not_found")
	}
	if record.Status != ArtifactStaged {
		return ArtifactRecord{}, fmt.Errorf("invalid_artifact_transition: %s 不能记录扫描", record.Status)
	}
	scan.ScannedAt = now.UTC()
	record.Scan, record.Status, record.UpdatedAt = &scan, ArtifactScanned, now.UTC()
	r.records[id] = record
	return record.clone(), nil
}

func (r *MemoryArtifactRegistry) Approve(actor Actor, id string, now time.Time) (ArtifactRecord, error) {
	if actor.ID == "" || (actor.Role != RoleExpert && actor.Role != RoleSecurity) {
		return ArtifactRecord{}, errors.New("forbidden: 仅 expert 或 security 可审批 Artifact")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[id]
	if !ok {
		return ArtifactRecord{}, errors.New("artifact_not_found")
	}
	if record.Status != ArtifactScanned {
		return ArtifactRecord{}, fmt.Errorf("invalid_artifact_transition: %s 不能审批", record.Status)
	}
	if record.IngestedBy == actor.ID {
		return ArtifactRecord{}, errors.New("forbidden: Artifact 登记者不得自审")
	}
	for _, approval := range record.Approvals {
		if approval.ActorID == actor.ID {
			return ArtifactRecord{}, errors.New("forbidden: 同一审批人不得同时满足多角色审批")
		}
		if approval.Role == actor.Role {
			return ArtifactRecord{}, fmt.Errorf("conflict: %s Artifact 审批已存在", actor.Role)
		}
	}
	record.Approvals = append(record.Approvals, ArtifactApproval{ActorID: actor.ID, Role: actor.Role, At: now.UTC()})
	if hasArtifactApprovals(record.Approvals, RoleExpert, RoleSecurity) {
		record.Status = ArtifactApproved
	}
	record.UpdatedAt = now.UTC()
	r.records[id] = record
	return record.clone(), nil
}

func (r *MemoryArtifactRegistry) Revoke(actor Actor, id, cause string, now time.Time) (ArtifactRecord, error) {
	if actor.ID == "" || actor.Role != RoleSecurity || cause == "" {
		return ArtifactRecord{}, errors.New("forbidden: 仅 security 可撤销 Artifact，且必须给出原因")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[id]
	if !ok {
		return ArtifactRecord{}, errors.New("artifact_not_found")
	}
	if record.Status == ArtifactRevoked {
		return record.clone(), nil
	}
	record.Status, record.RevokeCause, record.UpdatedAt = ArtifactRevoked, cause, now.UTC()
	r.records[id] = record
	return record.clone(), nil
}

func (r *MemoryArtifactRegistry) Get(id string) (ArtifactRecord, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[id]
	if !ok {
		return ArtifactRecord{}, errors.New("artifact_not_found")
	}
	return record.clone(), nil
}

func (r *MemoryArtifactRegistry) VerifyApproved(artifact Artifact) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[artifact.ID]
	if !ok || record.Status != ArtifactApproved || record.Digest != artifact.Digest {
		return errors.New("artifact_not_approved")
	}
	return nil
}

func validateArtifactMetadata(record ArtifactRecord) error {
	if record.ID == "" || record.Digest == "" || record.SizeBytes < 1 || record.MediaType == "" || record.Schema == "" {
		return errors.New("artifact_metadata_invalid")
	}
	provenance := record.Provenance
	if provenance.SourceType == "" || provenance.SourceRefDigest == "" || provenance.RedactionDigest == "" || provenance.CollectorID == "" || provenance.CollectionPolicy == "" || provenance.CollectedAt.IsZero() {
		return errors.New("artifact_provenance_incomplete")
	}
	return nil
}

func hasArtifactApprovals(approvals []ArtifactApproval, required ...Role) bool {
	roles := make(map[Role]bool, len(approvals))
	for _, approval := range approvals {
		roles[approval.Role] = true
	}
	for _, role := range required {
		if !roles[role] {
			return false
		}
	}
	return true
}

// ObjectRef 是对象存储 prepare/commit 流程中唯一传递的引用；不暴露原始内部 URI。
type ObjectRef struct {
	Key    string
	Digest string
	Size   int64
}

// BundleObjectStore 模拟受控 OCI/S3/WORM 存储的最小语义。生产实现必须把 Stage、Verify、
// Commit 与 PostgreSQL CAS/孤儿回收协同，而不能允许 Runtime 写入。
type BundleObjectStore interface {
	Prepare([]byte, string) (ObjectRef, error)
	Verify(ObjectRef) error
	Commit(ObjectRef) (ObjectRef, error)
	ReadPublished(ObjectRef) ([]byte, error)
	Abort(ObjectRef)
}

// MemoryBundleObjectStore 只用于测试 prepare/verify/commit 的失败语义，非生产对象存储实现。
type MemoryBundleObjectStore struct {
	mu        sync.Mutex
	sequence  int
	staged    map[string][]byte
	published map[string][]byte
}

func NewMemoryBundleObjectStore() *MemoryBundleObjectStore {
	return &MemoryBundleObjectStore{staged: map[string][]byte{}, published: map[string][]byte{}}
}

func (s *MemoryBundleObjectStore) Prepare(raw []byte, expectedDigest string) (ObjectRef, error) {
	if len(raw) == 0 || digestBytes(raw) != expectedDigest {
		return ObjectRef{}, errors.New("bundle_prepare_integrity_failed")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sequence++
	key := fmt.Sprintf("stage/%d", s.sequence)
	s.staged[key] = append([]byte(nil), raw...)
	return ObjectRef{Key: key, Digest: expectedDigest, Size: int64(len(raw))}, nil
}

func (s *MemoryBundleObjectStore) Verify(ref ObjectRef) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, ok := s.staged[ref.Key]
	if !ok || ref.Size != int64(len(raw)) || ref.Digest != digestBytes(raw) {
		return errors.New("bundle_verify_integrity_failed")
	}
	return nil
}

func (s *MemoryBundleObjectStore) Commit(ref ObjectRef) (ObjectRef, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, ok := s.staged[ref.Key]
	if !ok || ref.Size != int64(len(raw)) || ref.Digest != digestBytes(raw) {
		return ObjectRef{}, errors.New("bundle_commit_integrity_failed")
	}
	key := "bundles/" + ref.Digest
	if existing, exists := s.published[key]; exists && string(existing) != string(raw) {
		return ObjectRef{}, errors.New("bundle_immutable_conflict")
	}
	s.published[key] = append([]byte(nil), raw...)
	delete(s.staged, ref.Key)
	return ObjectRef{Key: key, Digest: ref.Digest, Size: ref.Size}, nil
}

func (s *MemoryBundleObjectStore) ReadPublished(ref ObjectRef) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, ok := s.published[ref.Key]
	if !ok || ref.Key != "bundles/"+ref.Digest || ref.Size != int64(len(raw)) || ref.Digest != digestBytes(raw) {
		return nil, errors.New("bundle_read_integrity_failed")
	}
	return append([]byte(nil), raw...), nil
}

func (s *MemoryBundleObjectStore) Abort(ref ObjectRef) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.staged, ref.Key)
}

func digestBytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return fmt.Sprintf("sha256:%x", sum[:])
}
