package database

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"hci_sim/internal/controlplane"
)

// ArtifactRepository 是 controlplane.ArtifactRegistry 的 PostgreSQL 实现。
// 语义与 MemoryArtifactRegistry 逐条对齐：仅 compiler 可登记、仅 security 可记录
// 四重扫描、expert+security 不同 actor 双审批、登记者不可自审、仅 security 可撤销。
// 状态迁移由 UPDATE ... WHERE status=... 守卫；审批唯一性由
// (artifact_id, actor_role) 与 (artifact_id, actor_id) 唯一约束兜底。
type ArtifactRepository struct {
	pool *pgxpool.Pool
}

func NewArtifactRepository(pool *pgxpool.Pool) (*ArtifactRepository, error) {
	if pool == nil {
		return nil, errors.New("hci_sim database pool is required")
	}
	return &ArtifactRepository{pool: pool}, nil
}

func (r *ArtifactRepository) Register(actor controlplane.Actor, record controlplane.ArtifactRecord, now time.Time) (controlplane.ArtifactRecord, error) {
	if actor.ID == "" || actor.Role != controlplane.RoleCompiler {
		return controlplane.ArtifactRecord{}, errors.New("forbidden: 仅受信任 compiler 可登记 Artifact metadata")
	}
	if err := validateArtifactRecord(record); err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	ctx := context.Background()
	// provenance 校验（source/redaction digest 等）与 Memory 实现保持一致。
	if err := validateArtifactProvenance(record); err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	row := r.pool.QueryRow(ctx, `
		INSERT INTO artifact.metadata
			(id, digest, size_bytes, media_type, schema_version, source_type, source_ref_digest,
			 redaction_digest, collection_policy, collector_id, collected_at, status, ingested_by, trace_id)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'staged', $12, NULLIF($13, ''))
		ON CONFLICT (id) DO NOTHING
	`, record.ID, record.Digest, record.SizeBytes, record.MediaType, record.Schema, record.Provenance.SourceType,
		record.Provenance.SourceRefDigest, record.Provenance.RedactionDigest, record.Provenance.CollectionPolicy,
		record.Provenance.CollectorID, record.Provenance.CollectedAt.UTC(), actor.ID, record.TraceID)
	if err := row.Scan(); err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return controlplane.ArtifactRecord{}, fmt.Errorf("artifact_register_failed: %w", err)
	}
	stored, err := r.Get(record.ID)
	if err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	// 比较 Provenance 关键字段，排除时间精度差异（CollectedAt 在 PG 中精度为微秒）
	provenanceMatch := stored.Provenance.SourceType == record.Provenance.SourceType &&
		stored.Provenance.SourceRefDigest == record.Provenance.SourceRefDigest &&
		stored.Provenance.RedactionDigest == record.Provenance.RedactionDigest &&
		stored.Provenance.CollectorID == record.Provenance.CollectorID &&
		stored.Provenance.CollectionPolicy == record.Provenance.CollectionPolicy
	if stored.Digest != record.Digest || stored.MediaType != record.MediaType || stored.Schema != record.Schema || !provenanceMatch {
		return controlplane.ArtifactRecord{}, errors.New("artifact_id_conflict")
	}
	if stored.Status == controlplane.ArtifactRevoked {
		return controlplane.ArtifactRecord{}, errors.New("artifact_revoked: 已撤销 Artifact 不可重新登记")
	}
	return stored, nil
}

func (r *ArtifactRepository) RecordScan(actor controlplane.Actor, id string, scan controlplane.ArtifactScanReport, now time.Time) (controlplane.ArtifactRecord, error) {
	if actor.ID == "" || actor.Role != controlplane.RoleSecurity {
		return controlplane.ArtifactRecord{}, errors.New("forbidden: 仅 security 可记录 Artifact 扫描")
	}
	if scan.ScannerRevision == "" || !scan.SecretScanPassed || !scan.PIIScanPassed || !scan.LicenseScanPassed || !scan.SchemaValid {
		return controlplane.ArtifactRecord{}, errors.New("artifact_scan_failed: secret、PII、license 与 schema 扫描必须全部通过")
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	tag := fmt.Sprintf("%s", scan.ScannerRevision)
	if _, err := tx.Exec(ctx, `
		INSERT INTO artifact.scan (artifact_id, scanner_revision, secret_scan_passed, pii_scan_passed, license_scan_passed, schema_valid, trace_id, scanned_at)
		VALUES ($1, $2, $3, $4, $5, $6, NULLIF($7, ''), $8)
	`, id, tag, scan.SecretScanPassed, scan.PIIScanPassed, scan.LicenseScanPassed, scan.SchemaValid, actor.ID, now.UTC()); err != nil {
		return controlplane.ArtifactRecord{}, fmt.Errorf("artifact_scan_record_failed: %w", err)
	}
	updated, err := tx.Exec(ctx, `
		UPDATE artifact.metadata SET status = 'scanned', version = version + 1, updated_at = $2
		WHERE id = $1 AND status = 'staged'
	`, id, now.UTC())
	if err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	if updated.RowsAffected() != 1 {
		if exists, err := artifactExists(tx, ctx, id); err == nil && exists {
			return controlplane.ArtifactRecord{}, fmt.Errorf("invalid_artifact_transition: %s 不能记录扫描", currentArtifactStatus(tx, ctx, id))
		}
		return controlplane.ArtifactRecord{}, errors.New("artifact_not_found")
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	return r.Get(id)
}

func (r *ArtifactRepository) Approve(actor controlplane.Actor, id string, now time.Time) (controlplane.ArtifactRecord, error) {
	if actor.ID == "" || (actor.Role != controlplane.RoleExpert && actor.Role != controlplane.RoleSecurity) {
		return controlplane.ArtifactRecord{}, errors.New("forbidden: 仅 expert 或 security 可审批 Artifact")
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var status, ingestedBy string
	if err := tx.QueryRow(ctx, `SELECT status, ingested_by FROM artifact.metadata WHERE id = $1 FOR UPDATE`, id).Scan(&status, &ingestedBy); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return controlplane.ArtifactRecord{}, errors.New("artifact_not_found")
		}
		return controlplane.ArtifactRecord{}, err
	}
	if status != "scanned" {
		return controlplane.ArtifactRecord{}, fmt.Errorf("invalid_artifact_transition: %s 不能审批", status)
	}
	if ingestedBy == actor.ID {
		return controlplane.ArtifactRecord{}, errors.New("forbidden: Artifact 登记者不得自审")
	}
	// 同 actor 多角色 / 同角色重复由唯一约束拒绝；语义与 Memory 实现一致。
	if _, err := tx.Exec(ctx, `
		INSERT INTO artifact.approval (artifact_id, actor_id, actor_role, decision, comment)
		VALUES ($1, $2, $3, 'approved', '')
	`, id, actor.ID, string(actor.Role)); err != nil {
		return controlplane.ArtifactRecord{}, fmt.Errorf("artifact_approval_conflict: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE artifact.metadata SET version = version + 1, updated_at = $2
		WHERE id = $1
	`, id, now.UTC()); err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	// 双角色齐备时迁移 approved；仍处 scanned 时幂等返回当前态。
	if _, err := tx.Exec(ctx, `
		UPDATE artifact.metadata SET status = 'approved', version = version + 1, updated_at = $2
		WHERE id = $1 AND status = 'scanned'
		  AND EXISTS (SELECT 1 FROM artifact.approval a WHERE a.artifact_id = $1 AND a.actor_role = 'expert' AND a.decision = 'approved')
		  AND EXISTS (SELECT 1 FROM artifact.approval a WHERE a.artifact_id = $1 AND a.actor_role = 'security' AND a.decision = 'approved')
	`, id, now.UTC()); err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	return r.Get(id)
}

func (r *ArtifactRepository) Revoke(actor controlplane.Actor, id, cause string, now time.Time) (controlplane.ArtifactRecord, error) {
	if actor.ID == "" || actor.Role != controlplane.RoleSecurity || cause == "" {
		return controlplane.ArtifactRecord{}, errors.New("forbidden: 仅 security 可撤销 Artifact，且必须给出原因")
	}
	ctx := context.Background()
	updated, err := r.pool.Exec(ctx, `
		UPDATE artifact.metadata
		SET status = 'revoked', revoke_reason = $2, version = version + 1, updated_at = $3
		WHERE id = $1
	`, id, cause, now.UTC())
	if err != nil {
		return controlplane.ArtifactRecord{}, err
	}
	if updated.RowsAffected() != 1 {
		return controlplane.ArtifactRecord{}, errors.New("artifact_not_found")
	}
	return r.Get(id)
}

func (r *ArtifactRepository) Get(id string) (controlplane.ArtifactRecord, error) {
	return scanArtifact(r.pool.QueryRow(context.Background(), `
		SELECT id, digest, size_bytes, media_type, schema_version, source_type, source_ref_digest,
		       redaction_digest, collection_policy, collector_id, collected_at, status, ingested_by,
		       COALESCE(trace_id, ''), version, COALESCE(revoke_reason, ''), created_at, updated_at,
		       COALESCE((
		       SELECT jsonb_build_object(
		           'scanner_revision', s.scanner_revision, 'secret_scan_passed', s.secret_scan_passed,
		           'pii_scan_passed', s.pii_scan_passed, 'license_scan_passed', s.license_scan_passed,
		           'schema_valid', s.schema_valid, 'scanned_at', s.scanned_at)
		       FROM artifact.scan s WHERE s.artifact_id = m.id ORDER BY s.scanned_at DESC, s.id DESC LIMIT 1
		       ), NULL),
		       COALESCE((
		       SELECT jsonb_agg(jsonb_build_object('actor_id', a.actor_id, 'actor_role', a.actor_role, 'decided_at', a.decided_at) ORDER BY a.decided_at, a.id)
		       FROM artifact.approval a WHERE a.artifact_id = m.id
		       ), '[]'::jsonb)
		FROM artifact.metadata m WHERE m.id = $1
	`, id))
}

// VerifyApproved 是 Compiler 绑定 Artifact 的唯一入口；不信任请求载荷里的状态。
func (r *ArtifactRepository) VerifyApproved(artifact controlplane.Artifact) error {
	var digest string
	var status controlplane.ArtifactStatus
	err := r.pool.QueryRow(context.Background(),
		`SELECT digest, status FROM artifact.metadata WHERE id = $1`, artifact.ID).Scan(&digest, &status)
	if err != nil {
		return errors.New("artifact_not_approved")
	}
	if status != controlplane.ArtifactApproved || digest != artifact.Digest {
		return errors.New("artifact_not_approved")
	}
	return nil
}

func scanArtifact(row pgx.Row) (controlplane.ArtifactRecord, error) {
	var record controlplane.ArtifactRecord
	var tag string
	var scanRaw []byte
	var approvalsRaw []byte
	var approvals []struct {
		ActorID   string    `json:"actor_id"`
		ActorRole string    `json:"actor_role"`
		DecidedAt time.Time `json:"decided_at"`
	}
	var version int
	err := row.Scan(&record.ID, &record.Digest, &record.SizeBytes, &record.MediaType, &tag,
		&record.Provenance.SourceType, &record.Provenance.SourceRefDigest, &record.Provenance.RedactionDigest,
		&record.Provenance.CollectionPolicy, &record.Provenance.CollectorID, &record.Provenance.CollectedAt,
		&record.Status, &record.IngestedBy, &record.TraceID, &version, &record.RevokeCause,
		&record.CreatedAt, &record.UpdatedAt, &scanRaw, &approvalsRaw)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return controlplane.ArtifactRecord{}, errors.New("artifact_not_found")
		}
		return controlplane.ArtifactRecord{}, err
	}
	record.Schema = tag
	// 兼容 PR788 早期适配器写入的 "media_type|schema" 复合值；新记录只写 schema。
	if prefix := record.MediaType + "|"; strings.HasPrefix(record.Schema, prefix) {
		record.Schema = strings.TrimPrefix(record.Schema, prefix)
	}
	if len(scanRaw) > 0 {
		var scan controlplane.ArtifactScanReport
		if err := json.Unmarshal(scanRaw, &scan); err == nil {
			record.Scan = &scan
		}
	}
	if err := json.Unmarshal(approvalsRaw, &approvals); err == nil {
		for _, approval := range approvals {
			record.Approvals = append(record.Approvals, controlplane.ArtifactApproval{
				ActorID: approval.ActorID, Role: controlplane.Role(approval.ActorRole), At: approval.DecidedAt,
			})
		}
	}
	return record, nil
}

func validateArtifactRecord(record controlplane.ArtifactRecord) error {
	if record.ID == "" || record.Digest == "" || record.SizeBytes < 1 || record.MediaType == "" || record.Schema == "" {
		return errors.New("artifact_metadata_invalid")
	}
	return nil
}

func validateArtifactProvenance(record controlplane.ArtifactRecord) error {
	provenance := record.Provenance
	if provenance.SourceType == "" || provenance.SourceRefDigest == "" || provenance.RedactionDigest == "" || provenance.CollectorID == "" || provenance.CollectionPolicy == "" || provenance.CollectedAt.IsZero() {
		return errors.New("artifact_provenance_incomplete")
	}
	return nil
}

func artifactExists(tx pgx.Tx, ctx context.Context, id string) (bool, error) {
	var exists bool
	err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM artifact.metadata WHERE id = $1)`, id).Scan(&exists)
	return exists, err
}

func currentArtifactStatus(tx pgx.Tx, ctx context.Context, id string) string {
	var status string
	if err := tx.QueryRow(ctx, `SELECT status FROM artifact.metadata WHERE id = $1`, id).Scan(&status); err != nil {
		return "unknown"
	}
	return status
}
