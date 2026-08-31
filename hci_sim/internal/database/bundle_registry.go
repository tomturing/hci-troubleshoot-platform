package database

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/fixture"
)

// BundleRegistry 是 controlplane.Registry 的 PostgreSQL 实现。
// 语义与 MemoryRegistry 对齐：Compile 幂等（指纹去重）、Validate/Approve/Publish
// 状态迁移 CAS、MarkStale 写入统一 Outbox、GetPublished 验证对象完整性。
// 依赖注入：ArtifactGate 用于 realistic 路由的 Artifact 绑定校验；
// BundleObjectStore 用于 Manifest 字节存储（生产需对接 OCI/S3）。
type BundleRegistry struct {
	pool         *pgxpool.Pool
	artifactGate controlplane.ArtifactGate
	objectStore  controlplane.BundleObjectStore
}

func NewBundleRegistry(pool *pgxpool.Pool, artifactGate controlplane.ArtifactGate, objectStore controlplane.BundleObjectStore) (*BundleRegistry, error) {
	if pool == nil {
		return nil, errors.New("hci_sim database pool is required")
	}
	if objectStore == nil {
		return nil, errors.New("BundleObjectStore is required（Memory 实现仅用于测试）")
	}
	return &BundleRegistry{pool: pool, artifactGate: artifactGate, objectStore: objectStore}, nil
}

func (r *BundleRegistry) Compile(actor controlplane.Actor, input controlplane.CompileInput, manifest fixture.Manifest, now time.Time) (controlplane.BundleRecord, error) {
	return r.compile(actor, input, manifest, now, "")
}

// compile 将 BundleBuild、依赖、统一 outbox 和可选的父 Draft 淘汰收敛在同一事务。
// supersedeParentDigest 非空时必须先锁定父行，避免两个并发修订都成为当前 Draft。
func (r *BundleRegistry) compile(actor controlplane.Actor, input controlplane.CompileInput, manifest fixture.Manifest, now time.Time, supersedeParentDigest string) (controlplane.BundleRecord, error) {
	if actor.Role != controlplane.RoleCompiler || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: 仅 compiler 身份可创建 draft")
	}
	if len(input.RouteSources) == 0 {
		input.RouteSources = make([]controlplane.RouteSource, 0, len(manifest.Routes))
		for _, route := range manifest.Routes {
			sourceRef := route.SignalID
			if sourceRef == "" {
				sourceRef = route.ID
			}
			input.RouteSources = append(input.RouteSources, controlplane.RouteSource{
				RouteID: route.ID, SignalID: route.SignalID, SourceType: "kbd_signal_contract",
				SourceRef: sourceRef, SourceDigest: input.SignalsDigest,
			})
		}
	}
	fingerprint, err := input.Fingerprint()
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if input.BundleInputDigest == "" {
		input.BundleInputDigest = fingerprint
	}
	if manifest.KBD.SupportID != input.SupportID || manifest.KBD.Revision != input.KBDRevision || manifest.KBD.Checksum != input.KBDChecksum || manifest.Contracts.ToolRevision != input.ToolContractRevision || manifest.Contracts.PolicyRevision != input.PolicyRevision {
		return controlplane.BundleRecord{}, errors.New("capability_gap: manifest 与已冻结编译输入不一致")
	}
	if hasRealisticRoute(manifest) && len(input.Artifacts) == 0 {
		return controlplane.BundleRecord{}, errors.New("capability_gap: positive-realistic route 缺少批准 Artifact provenance")
	}
	if len(input.Artifacts) > 0 && r.artifactGate == nil {
		return controlplane.BundleRecord{}, errors.New("capability_gap: Artifact 审批 Registry 未配置")
	}
	for _, artifact := range input.Artifacts {
		if err := r.artifactGate.VerifyApproved(artifact); err != nil {
			return controlplane.BundleRecord{}, fmt.Errorf("capability_gap: Artifact %s 不可绑定: %w", artifact.ID, err)
		}
	}
	manifest.Bundle.Status = "published"
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	raw, err := json.Marshal(manifest)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if _, err := fixture.Parse(raw); err != nil {
		return controlplane.BundleRecord{}, fmt.Errorf("manifest lint 失败: %w", err)
	}
	object, err := r.objectStore.Prepare(raw, digestBytes(raw))
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := r.objectStore.Verify(object); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, err
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	parentStatus := ""
	if supersedeParentDigest != "" {
		if input.ParentBundleDigest != supersedeParentDigest {
			r.objectStore.Abort(object)
			return controlplane.BundleRecord{}, errors.New("parent_bundle_digest_mismatch")
		}
		if err := tx.QueryRow(ctx, `SELECT status FROM fixture.bundle WHERE digest = $1 FOR UPDATE`, supersedeParentDigest).Scan(&parentStatus); err != nil {
			r.objectStore.Abort(object)
			if errors.Is(err, pgx.ErrNoRows) {
				return controlplane.BundleRecord{}, errors.New("bundle_not_found")
			}
			return controlplane.BundleRecord{}, fmt.Errorf("lock parent bundle: %w", err)
		}
		if parentStatus != string(controlplane.BundleDraft) && parentStatus != string(controlplane.BundleValidated) && parentStatus != string(controlplane.BundlePublished) {
			r.objectStore.Abort(object)
			return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s 不能修订", parentStatus)
		}
	}
	scenarioID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("controlplane:"+fingerprint))
	variant := primaryVariant(manifest)
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.scenario (id, support_id, kbd_revision, variant, input_fingerprint, status, package_snapshot_digest, knowledge_snapshot_digest)
		VALUES ($1, $2, $3, $4, $5, 'indexed', NULLIF($6, ''), NULLIF($7, ''))
		ON CONFLICT (input_fingerprint) DO UPDATE SET updated_at = now()
	`, scenarioID, input.SupportID, input.KBDRevision, variant, fingerprint, input.PackageSnapshotDigest, input.KBDChecksum); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, fmt.Errorf("upsert scenario: %w", err)
	}
	inputJSON, _ := json.Marshal(input)
	bundleID := uuid.NewSHA1(uuid.NameSpaceURL, []byte(manifest.Bundle.Digest))
	workspaceID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("hci:bundle-workspace:"+input.SupportID))
	var existingDigest string
	var existingStatus string
	if err := tx.QueryRow(ctx, `SELECT digest, status FROM fixture.bundle WHERE input_fingerprint = $1`, fingerprint).Scan(&existingDigest, &existingStatus); err == nil {
		r.objectStore.Abort(object)
		if existingDigest != manifest.Bundle.Digest {
			return controlplane.BundleRecord{}, errors.New("compiler_nondeterministic_output: 相同冻结输入生成了不同 Bundle")
		}
		if existingStatus == string(controlplane.BundleRetired) || existingStatus == string(controlplane.BundleStale) {
			if _, err := tx.Exec(ctx, `
				UPDATE fixture.bundle
				SET status = 'draft', stale_reason = NULL, version = version + 1, updated_at = $2
				WHERE digest = $1
			`, existingDigest, now.UTC()); err != nil {
				return controlplane.BundleRecord{}, fmt.Errorf("reactivate non-draft bundle: %w", err)
			}
		}
		if err := supersedeParentDraft(ctx, tx, supersedeParentDigest, manifest.Bundle.Digest, parentStatus, now); err != nil {
			return controlplane.BundleRecord{}, err
		}
		if err := tx.Commit(ctx); err != nil {
			return controlplane.BundleRecord{}, err
		}
		return r.Get(manifest.Bundle.Digest)
	} else if !errors.Is(err, pgx.ErrNoRows) {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO fixture.bundle (id, scenario_id, revision, digest, schema_version, object_uri, object_digest, size_bytes, status, created_by, input_fingerprint, compile_input, bundle_input_digest, package_snapshot_digest, knowledge_release_id, compiler_revision, workspace_id, source_knowledge_revision_no)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'draft', $9, $10, $11, $12, NULLIF($13, ''), NULLIF($14, ''), $15, $16, $17)
		ON CONFLICT (digest) DO NOTHING
	`, bundleID, scenarioID, input.KBDRevision, manifest.Bundle.Digest, manifest.SchemaVersion,
		object.Key, object.Digest, object.Size, actor.ID, fingerprint, inputJSON, input.BundleInputDigest,
		input.PackageSnapshotDigest, input.KnowledgeReleaseID, input.CompilerRevision, workspaceID, input.KBDRevision); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, fmt.Errorf("insert bundle: %w", err)
	}
	if err := writeDependencies(ctx, tx, bundleID, input.Dependencies); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, err
	}
	if err := enqueueOutbox(ctx, tx, "bundle", "bundle_build", manifest.Bundle.Digest, nil, "bundle.compiled", fingerprint, ""); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, fmt.Errorf("enqueue bundle compile event: %w", err)
	}
	if err := supersedeParentDraft(ctx, tx, supersedeParentDigest, manifest.Bundle.Digest, parentStatus, now); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		r.objectStore.Abort(object)
		return controlplane.BundleRecord{}, err
	}
	return controlplane.BundleRecord{
		Digest:           manifest.Bundle.Digest,
		InputFingerprint: fingerprint,
		Input:            input,
		Manifest:         raw,
		Object:           object,
		Status:           controlplane.BundleDraft,
		Creator:          actor.ID,
		CreatedAt:        now.UTC(),
		UpdatedAt:        now.UTC(),
	}, nil
}

func supersedeParentDraft(ctx context.Context, tx pgx.Tx, parentDigest, childDigest, parentStatus string, now time.Time) error {
	if parentDigest == "" || parentStatus != string(controlplane.BundleDraft) {
		return nil
	}
	result, err := tx.Exec(ctx, `
		UPDATE fixture.bundle
		SET status = 'stale', stale_reason = $1, version = version + 1, updated_at = $2
		WHERE digest = $3 AND status = 'draft'
	`, "superseded_by_revision:"+childDigest, now.UTC(), parentDigest)
	if err != nil {
		return fmt.Errorf("supersede parent draft: %w", err)
	}
	if result.RowsAffected() != 1 {
		return errors.New("bundle_parent_cas_conflict")
	}
	return nil
}

// ReviseDraft 将专家修改固化为新 Draft，而不是覆盖既有对象或已发布 Bundle。
// 新 Draft 写入成功后，父 Draft（若仍为 draft 状态）被降级为 stale，确保
// List() 返回的 draft 列表始终只有一个条目，避免前端误判"存在多个 Draft"。
func (r *BundleRegistry) ReviseDraft(actor controlplane.Actor, parentDigest string, manifest fixture.Manifest, reason string, now time.Time) (controlplane.BundleRecord, error) {
	if actor.Role != controlplane.RoleExpert || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: 仅 expert 可修订 Draft")
	}
	parent, err := r.Get(parentDigest)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if parent.Status != controlplane.BundleDraft && parent.Status != controlplane.BundleValidated && parent.Status != controlplane.BundlePublished {
		return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s 不能修订", parent.Status)
	}
	if reason == "" {
		return controlplane.BundleRecord{}, errors.New("draft_edit_reason_required")
	}
	manifest.Bundle.Status = "published"
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	if manifest.Bundle.Digest == parent.Digest {
		return controlplane.BundleRecord{}, errors.New("draft_edit_no_changes")
	}
	input := parent.Input
	input.ParentBundleDigest = parent.Digest
	input.DraftRevision++
	input.EditReason = reason
	input.BundleInputDigest = ""
	return r.compile(
		controlplane.Actor{ID: actor.ID, Role: controlplane.RoleCompiler},
		input,
		manifest,
		now,
		parentDigest,
	)
}

// Retire 将 Draft 或 Stale Bundle 标记为 retired。对象、审批和运行引用保持不变；
// 同时拒绝任何仍被 Runtime activation 指针使用的 Bundle。
func (r *BundleRegistry) Retire(actor controlplane.Actor, digest string, now time.Time) (controlplane.BundleRecord, error) {
	if actor.Role != controlplane.RoleExpert || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: 仅 expert 可归档 Bundle")
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	updated, err := tx.Exec(ctx, `
		UPDATE fixture.bundle SET status = 'retired', version = version + 1, updated_at = $2
		WHERE digest = $1 AND status IN ('draft', 'stale')
		  AND NOT EXISTS (
			SELECT 1 FROM fixture.bundle_activation
			WHERE desired_digest = $1 OR active_digest = $1 OR previous_digest = $1
		  )
	`, digest, now.UTC())
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if updated.RowsAffected() != 1 {
		var status string
		if err := tx.QueryRow(ctx, `SELECT status FROM fixture.bundle WHERE digest = $1`, digest).Scan(&status); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return controlplane.BundleRecord{}, errors.New("bundle_not_found")
			}
			return controlplane.BundleRecord{}, err
		}
		if status == "draft" || status == "stale" {
			return controlplane.BundleRecord{}, errors.New("bundle_retire_blocked_by_runtime_activation")
		}
		return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s 不能归档", status)
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.BundleRecord{}, err
	}
	return r.Get(digest)
}

func (r *BundleRegistry) Validate(actor controlplane.Actor, digest string, report controlplane.ValidationReport, now time.Time) (controlplane.BundleRecord, error) {
	if actor.Role != controlplane.RoleCompiler || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: 仅 compiler 身份可验证 draft")
	}
	if !report.MutationDetected || !report.SecretScanPassed || !report.IndependentProof {
		return controlplane.BundleRecord{}, errors.New("validation_failed: mutation、secret scan 与独立证据均为发布前置条件")
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	updated, err := tx.Exec(ctx, `UPDATE fixture.bundle SET status = 'validated', version = version + 1, updated_at = $2 WHERE digest = $1 AND status = 'draft'`, digest, now.UTC())
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if updated.RowsAffected() != 1 {
		return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s", currentBundleStatus(tx, ctx, digest))
	}
	if err := r.insertApproval(ctx, tx, digest, "validate", actor.ID, actor.Role, now); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.BundleRecord{}, err
	}
	return r.Get(digest)
}

func (r *BundleRegistry) Approve(actor controlplane.Actor, digest string, now time.Time) (controlplane.BundleRecord, error) {
	if (actor.Role != controlplane.RoleExpert && actor.Role != controlplane.RoleSecurity) || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: 仅 expert 或 security 可审批")
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var status string
	var creator string
	if err := tx.QueryRow(ctx, `SELECT b.status, b.created_by FROM fixture.bundle b JOIN control_plane.scenario s ON s.id = b.scenario_id WHERE b.digest = $1 FOR UPDATE`, digest).Scan(&status, &creator); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return controlplane.BundleRecord{}, errors.New("bundle_not_found")
		}
		return controlplane.BundleRecord{}, err
	}
	if status != "validated" {
		return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s 不能审批", status)
	}
	if actor.ID == creator {
		return controlplane.BundleRecord{}, errors.New("forbidden: compiler 不得自审")
	}
	var existingActorRole string
	err = tx.QueryRow(ctx, `
		SELECT actor_role FROM fixture.approval
		WHERE bundle_id = (SELECT id FROM fixture.bundle WHERE digest = $1)
		  AND stage = 'approve' AND decision = 'approved' AND actor_id = $2
		ORDER BY decided_at DESC, id DESC LIMIT 1
	`, digest, actor.ID).Scan(&existingActorRole)
	if err == nil {
		return controlplane.BundleRecord{}, errors.New("forbidden: 同一审批人不得同时满足多角色审批")
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return controlplane.BundleRecord{}, err
	}
	var existingRoleActor string
	err = tx.QueryRow(ctx, `
		SELECT actor_id FROM fixture.approval
		WHERE bundle_id = (SELECT id FROM fixture.bundle WHERE digest = $1)
		  AND stage = 'approve' AND decision = 'approved' AND actor_role = $2
		ORDER BY decided_at DESC, id DESC LIMIT 1
	`, digest, string(actor.Role)).Scan(&existingRoleActor)
	if err == nil {
		return controlplane.BundleRecord{}, fmt.Errorf("conflict: %s Bundle 审批已存在", actor.Role)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return controlplane.BundleRecord{}, err
	}
	if err := r.insertApproval(ctx, tx, digest, "approve", actor.ID, actor.Role, now); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if _, err := tx.Exec(ctx, `
		UPDATE fixture.bundle SET status = 'approved', version = version + 1, updated_at = $2
		WHERE digest = $1 AND status = 'validated'
		  AND EXISTS (SELECT 1 FROM fixture.approval WHERE bundle_id = (SELECT id FROM fixture.bundle WHERE digest = $1) AND stage = 'approve' AND actor_role = 'expert')
		  AND EXISTS (SELECT 1 FROM fixture.approval WHERE bundle_id = (SELECT id FROM fixture.bundle WHERE digest = $1) AND stage = 'approve' AND actor_role = 'security')
	`, digest, now.UTC()); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.BundleRecord{}, err
	}
	return r.Get(digest)
}

func (r *BundleRegistry) Publish(actor controlplane.Actor, digest string, now time.Time) (controlplane.BundleRecord, error) {
	if actor.Role != controlplane.RolePublisher || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: 仅 publisher 可发布")
	}
	ctx := context.Background()
	record, err := r.Get(digest)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if record.Status != controlplane.BundleApproved {
		return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s → %s", record.Status, controlplane.BundlePublished)
	}
	published, err := r.objectStore.Commit(record.Object)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `UPDATE fixture.bundle SET status = 'published', object_uri = $2, object_digest = $3, size_bytes = $4, version = version + 1, updated_at = $5 WHERE digest = $1`,
		digest, published.Key, published.Digest, published.Size, now.UTC()); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := r.insertApproval(ctx, tx, digest, "publish", actor.ID, actor.Role, now); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.BundleRecord{}, err
	}
	return r.Get(digest)
}

// PublishInternalFast 将自动校验与专家发布合并为内部功能仿真的最短路径。
// Compile/Revise 已经完成 Manifest lint、secret scan 和对象 digest 校验；这里以 CAS
// 防止并发发布覆盖状态，并保留 validate/publish 两个可追溯事件。
func (r *BundleRegistry) PublishInternalFast(actor controlplane.Actor, digest string, now time.Time) (controlplane.BundleRecord, error) {
	if actor.Role != controlplane.RoleExpert || actor.ID == "" {
		return controlplane.BundleRecord{}, errors.New("forbidden: Internal Fast Path 仅允许已认证专家发布")
	}
	ctx := context.Background()
	record, err := r.Get(digest)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if record.Status != controlplane.BundleDraft && record.Status != controlplane.BundleValidated {
		return controlplane.BundleRecord{}, fmt.Errorf("invalid_transition: %s → %s", record.Status, controlplane.BundlePublished)
	}
	published, err := r.objectStore.Commit(record.Object)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	updated, err := tx.Exec(ctx, `
		UPDATE fixture.bundle
		SET status = 'published', object_uri = $2, object_digest = $3, size_bytes = $4,
		    version = version + 1, updated_at = $5
		WHERE digest = $1 AND status IN ('draft', 'validated')
	`, digest, published.Key, published.Digest, published.Size, now.UTC())
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if updated.RowsAffected() != 1 {
		return controlplane.BundleRecord{}, errors.New("bundle_fast_publish_conflict")
	}
	if err := r.insertApproval(ctx, tx, digest, "validate", actor.ID, actor.Role, now); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := r.insertApproval(ctx, tx, digest, "publish", actor.ID, actor.Role, now); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return controlplane.BundleRecord{}, err
	}
	return r.Get(digest)
}

func (r *BundleRegistry) MarkStale(actor controlplane.Actor, changed controlplane.Dependency, reason string, now time.Time) ([]controlplane.BundleRecord, error) {
	if actor.ID == "" || (actor.Role != controlplane.RoleCompiler && actor.Role != controlplane.RoleSecurity) || reason == "" {
		return nil, errors.New("forbidden: 仅受信任控制面可标记 stale")
	}
	ctx := context.Background()
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `
		UPDATE fixture.bundle b SET status = 'stale', stale_reason = $1, version = b.version + 1, updated_at = $2
		FROM fixture.dependency d
		WHERE b.id = d.bundle_id AND b.status = 'published'
		  AND d.dependency_type = $3 AND d.dependency_id = $4 AND d.revision <> $5
		RETURNING b.id, b.digest
	`, reason, now.UTC(), changed.Type, changed.ID, changed.Revision)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	type staleBundle struct {
		id     uuid.UUID
		digest string
	}
	staleBundles := make([]staleBundle, 0)
	for rows.Next() {
		var item staleBundle
		if err := rows.Scan(&item.id, &item.digest); err != nil {
			return nil, err
		}
		staleBundles = append(staleBundles, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()
	for _, stale := range staleBundles {
		payload, err := json.Marshal(map[string]string{
			"dependency_type": changed.Type, "dependency_id": changed.ID,
			"dependency_revision": changed.Revision, "dependency_digest": changed.Digest,
			"reason": reason, "bundle_digest": stale.digest,
		})
		if err != nil {
			return nil, err
		}
		if err := enqueueOutbox(ctx, tx, "fixture_stale", "fixture_bundle", stale.digest, nil, "bundle.stale", digestBytes(payload), ""); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO audit.entity_event
				(entity_type, entity_id, action, actor_id, trace_id, before_state, after_state)
			VALUES ('fixture_bundle', $1, 'bundle.stale', $2, NULLIF($3, ''),
			        jsonb_build_object('status', 'published', 'digest', $4::text),
			        jsonb_build_object('status', 'stale', 'digest', $4::text))
		`, stale.id, actor.ID, "", stale.digest); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	var stale []controlplane.BundleRecord
	for _, item := range staleBundles {
		rec, err := r.Get(item.digest)
		if err != nil {
			return nil, err
		}
		stale = append(stale, rec)
	}
	return stale, nil
}

func (r *BundleRegistry) GetPublished(digest string) (controlplane.BundleRecord, error) {
	record, err := r.Get(digest)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if record.Status != controlplane.BundlePublished {
		return controlplane.BundleRecord{}, fmt.Errorf("bundle_not_runnable: %s", record.Status)
	}
	raw, err := r.objectStore.ReadPublished(record.Object)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	router, err := fixture.Parse(raw)
	if err != nil {
		return controlplane.BundleRecord{}, fmt.Errorf("bundle_integrity_failed: %w", err)
	}
	if router.KBD().SupportID != record.Input.SupportID || router.KBD().Revision != record.Input.KBDRevision {
		return controlplane.BundleRecord{}, errors.New("bundle_integrity_failed: 对象与冻结输入不一致")
	}
	return record, nil
}

func (r *BundleRegistry) ResolvePublished(supportID, variant, node, container string) (controlplane.BundleRecord, error) {
	ctx := context.Background()
	rows, err := r.pool.Query(ctx, `
		SELECT b.digest
		FROM fixture.bundle b
		JOIN control_plane.scenario s ON s.id = b.scenario_id
		WHERE s.support_id = $1 AND b.status = 'published' AND b.compile_input IS NOT NULL
		ORDER BY b.created_at DESC, b.digest
	`, supportID)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	defer rows.Close()
	var selected *controlplane.BundleRecord
	for rows.Next() {
		var digest string
		if err := rows.Scan(&digest); err != nil {
			return controlplane.BundleRecord{}, err
		}
		record, err := r.GetPublished(digest)
		if err != nil {
			return controlplane.BundleRecord{}, err
		}
		router, err := fixture.Parse(record.Manifest)
		if err != nil {
			return controlplane.BundleRecord{}, err
		}
		for _, route := range router.Routes() {
			if route.Variant != variant || route.RouteKey.Node != node || route.RouteKey.Container != container {
				continue
			}
			if selected != nil && selected.Digest != record.Digest {
				return controlplane.BundleRecord{}, errors.New("bundle_resolution_ambiguous")
			}
			copy := record
			selected = &copy
			break
		}
	}
	if err := rows.Err(); err != nil {
		return controlplane.BundleRecord{}, err
	}
	if selected == nil {
		return controlplane.BundleRecord{}, errors.New("bundle_resolution_missing")
	}
	return *selected, nil
}

func (r *BundleRegistry) Get(digest string) (controlplane.BundleRecord, error) {
	ctx := context.Background()
	row := r.pool.QueryRow(ctx, `
		SELECT b.digest, b.input_fingerprint, b.compile_input, b.object_uri, b.object_digest, b.size_bytes, b.status, b.created_by, b.stale_reason, b.created_at, b.updated_at,
		       COALESCE((
		       SELECT jsonb_agg(jsonb_build_object('actor_id', a.actor_id, 'role', a.actor_role, 'at', a.decided_at) ORDER BY a.decided_at, a.id)
		       FROM fixture.approval a WHERE a.bundle_id = b.id AND a.decision = 'approved'
		       ), '[]'::jsonb)
		FROM fixture.bundle b WHERE b.digest = $1
	`, digest)
	record, err := scanBundle(row)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	raw, err := r.objectStore.Read(record.Object)
	if err != nil {
		return controlplane.BundleRecord{}, err
	}
	if record.Object.Digest != digestBytes(raw) || record.Object.Size != int64(len(raw)) {
		return controlplane.BundleRecord{}, errors.New("bundle_integrity_failed: metadata 与对象 payload 不一致")
	}
	record.Manifest = raw
	return record, nil
}

func (r *BundleRegistry) List(supportID string) ([]controlplane.BundleRecord, error) {
	ctx := context.Background()
	rows, err := r.pool.Query(ctx, `
		SELECT b.digest
		FROM fixture.bundle b
		JOIN control_plane.scenario s ON s.id = b.scenario_id
		WHERE ($1 = '' OR s.support_id = $1) AND b.compile_input IS NOT NULL AND b.status <> 'retired'
		ORDER BY b.updated_at DESC, b.digest
	`, supportID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records := make([]controlplane.BundleRecord, 0)
	for rows.Next() {
		var digest string
		if err := rows.Scan(&digest); err != nil {
			return nil, err
		}
		record, err := r.Get(digest)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, rows.Err()
}

func scanBundle(row pgx.Row) (controlplane.BundleRecord, error) {
	var record controlplane.BundleRecord
	var inputJSON []byte
	var approvalsRaw []byte
	var staleReason *string
	err := row.Scan(&record.Digest, &record.InputFingerprint, &inputJSON, &record.Object.Key, &record.Object.Digest, &record.Object.Size, &record.Status, &record.Creator, &staleReason, &record.CreatedAt, &record.UpdatedAt, &approvalsRaw)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return controlplane.BundleRecord{}, errors.New("bundle_not_found")
		}
		return controlplane.BundleRecord{}, err
	}
	if staleReason != nil {
		record.StaleReason = *staleReason
	}
	if len(inputJSON) > 0 {
		if err := json.Unmarshal(inputJSON, &record.Input); err != nil {
			return controlplane.BundleRecord{}, fmt.Errorf("bundle_input_corrupt: %w", err)
		}
	}
	var approvals []struct {
		ActorID string    `json:"actor_id"`
		Role    string    `json:"role"`
		At      time.Time `json:"at"`
	}
	if err := json.Unmarshal(approvalsRaw, &approvals); err == nil {
		for _, approval := range approvals {
			record.Approvals = append(record.Approvals, controlplane.Approval{
				ActorID: approval.ActorID, Role: controlplane.Role(approval.Role), At: approval.At,
			})
		}
	}
	return record, nil
}

func (r *BundleRegistry) insertApproval(ctx context.Context, tx pgx.Tx, digest, stage, actorID string, actorRole controlplane.Role, now time.Time) error {
	var bundleID uuid.UUID
	if err := tx.QueryRow(ctx, `SELECT id FROM fixture.bundle WHERE digest = $1`, digest).Scan(&bundleID); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO fixture.approval (bundle_id, stage, actor_id, actor_role, decision, decided_at) VALUES ($1, $2, $3, $4, 'approved', $5)`, bundleID, stage, actorID, string(actorRole), now.UTC()); err != nil {
		return err
	}
	return nil
}

func writeDependencies(ctx context.Context, tx pgx.Tx, bundleID uuid.UUID, deps []controlplane.Dependency) error {
	for _, dep := range deps {
		if _, err := tx.Exec(ctx, `
			INSERT INTO fixture.dependency (bundle_id, dependency_type, dependency_id, revision, digest)
			VALUES ($1, $2, $3, $4, $5)
			ON CONFLICT DO NOTHING
		`, bundleID, dep.Type, dep.ID, dep.Revision, dep.Digest); err != nil {
			return err
		}
	}
	return nil
}

func primaryVariant(manifest fixture.Manifest) string {
	for _, route := range manifest.Routes {
		if route.Variant != "" {
			return route.Variant
		}
	}
	return "positive-minimal"
}

func currentBundleStatus(tx pgx.Tx, ctx context.Context, digest string) string {
	var status string
	if err := tx.QueryRow(ctx, `SELECT status FROM fixture.bundle WHERE digest = $1`, digest).Scan(&status); err != nil {
		return "unknown"
	}
	return status
}

func hasRealisticRoute(manifest fixture.Manifest) bool {
	for _, route := range manifest.Routes {
		if route.Variant == "positive-realistic" {
			return true
		}
	}
	return false
}

func digestBytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return fmt.Sprintf("sha256:%x", sum[:])
}
