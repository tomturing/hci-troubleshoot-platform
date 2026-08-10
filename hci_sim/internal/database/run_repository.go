package database

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrIdempotencyConflict = errors.New("idempotency_conflict")
	ErrRunVersionConflict  = errors.New("run_version_conflict")
)

// RunInput 是控制面创建 TestRun 时被冻结的字段集合。Lease 明文、SSH 密码和
// 原始 Artifact 不属于该结构，也不能进入数据库。
type RunInput struct {
	ExternalID       string
	SupportID        string
	KBDRevision      int
	Variant          string
	BundleDigest     string
	ExecutionMode    string
	IdempotencyKey   string
	RequestDigest    string
	Deadline         time.Time
	InputFingerprint string
}

type RunRecord struct {
	ExternalID     string
	SupportID      string
	KBDRevision    int
	BundleDigest   string
	Variant        string
	ExecutionMode  string
	IdempotencyKey string
	RequestDigest  string
	Status         string
	Version        int
	Deadline       time.Time
}

type RunRepository struct {
	pool *pgxpool.Pool
}

func NewRunRepository(pool *pgxpool.Pool) (*RunRepository, error) {
	if pool == nil {
		return nil, errors.New("hci_sim database pool is required")
	}
	return &RunRepository{pool: pool}, nil
}

// Create 幂等创建 Scenario + Run。相同 idempotency_key 且 request_digest 相同
// 时返回原记录；同 key 不同请求显式拒绝，不能覆盖既有 TestRun。
func (r *RunRepository) Create(ctx context.Context, input RunInput) (RunRecord, error) {
	if err := validateRunInput(input); err != nil {
		return RunRecord{}, err
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return RunRecord{}, fmt.Errorf("begin hci_sim run transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	scenarioID := uuid.NewSHA1(uuid.NameSpaceURL, []byte(input.InputFingerprint))
	runID := uuid.New()
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.scenario
			(id, support_id, kbd_revision, variant, input_fingerprint, status)
		VALUES ($1, $2, $3, $4, $5, 'published')
		ON CONFLICT (input_fingerprint) DO UPDATE SET updated_at = now()
	`, scenarioID, input.SupportID, input.KBDRevision, input.Variant, input.InputFingerprint); err != nil {
		return RunRecord{}, fmt.Errorf("upsert hci_sim scenario: %w", err)
	}

	row := tx.QueryRow(ctx, `
		INSERT INTO control_plane.run
			(id, external_id, support_id, kbd_revision, scenario_id, bundle_digest,
			 variant, execution_mode, status, idempotency_key, request_digest, deadline_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'requested', $9, $10, $11)
		ON CONFLICT (idempotency_key) DO NOTHING
		RETURNING external_id, support_id, kbd_revision, bundle_digest, variant,
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest
	`, runID, input.ExternalID, input.SupportID, input.KBDRevision, scenarioID, input.BundleDigest,
		input.Variant, input.ExecutionMode, input.IdempotencyKey, input.RequestDigest, input.Deadline.UTC())
	record, scanErr := scanRun(row)
	if scanErr != nil {
		if !errors.Is(scanErr, pgx.ErrNoRows) {
			return RunRecord{}, fmt.Errorf("insert hci_sim run: %w", scanErr)
		}
		record, scanErr = r.getByIdempotency(ctx, tx, input.IdempotencyKey)
		if scanErr != nil {
			return RunRecord{}, scanErr
		}
		if record.RequestDigest != input.RequestDigest || record.BundleDigest != input.BundleDigest {
			return RunRecord{}, ErrIdempotencyConflict
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return RunRecord{}, fmt.Errorf("commit hci_sim run: %w", err)
	}
	return record, nil
}

func (r *RunRepository) Get(ctx context.Context, externalID string) (RunRecord, error) {
	if externalID == "" {
		return RunRecord{}, errors.New("external TestRun ID is required")
	}
	return scanRun(r.pool.QueryRow(ctx, `
		SELECT external_id, support_id, kbd_revision, bundle_digest, variant,
		       execution_mode, status, version, deadline_at, idempotency_key, request_digest
		FROM control_plane.run WHERE external_id = $1
	`, externalID))
}

// UpdateStatusCAS 是所有状态变更的唯一入口；version 不匹配时返回并发冲突，
// 不允许旧 Worker 覆盖新状态。
func (r *RunRepository) UpdateStatusCAS(ctx context.Context, externalID string, expectedVersion int, status string) (RunRecord, error) {
	if externalID == "" || expectedVersion < 1 || status == "" {
		return RunRecord{}, errors.New("invalid status CAS request")
	}
	record, err := scanRun(r.pool.QueryRow(ctx, `
		UPDATE control_plane.run
		SET status = $1, version = version + 1, updated_at = now()
		WHERE external_id = $2 AND version = $3
		RETURNING external_id, support_id, kbd_revision, bundle_digest, variant,
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest
	`, status, externalID, expectedVersion))
	if errors.Is(err, pgx.ErrNoRows) {
		return RunRecord{}, ErrRunVersionConflict
	}
	return record, err
}

// RecordLease durably binds the first short-lived lease to the TestRun. The
// lease token itself never enters PostgreSQL; only a one-way JTI hash is kept.
// A retry for an already leased attempt is idempotent when the hash matches.
func (r *RunRepository) RecordLease(ctx context.Context, externalID string, expectedVersion, attemptNo int, runtimeID, jtiHash string) (RunRecord, error) {
	if externalID == "" || expectedVersion < 1 || attemptNo < 1 || runtimeID == "" || jtiHash == "" {
		return RunRecord{}, errors.New("invalid lease record")
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return RunRecord{}, fmt.Errorf("begin hci_sim lease transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var runID uuid.UUID
	var status string
	var version int
	if err := tx.QueryRow(ctx, `SELECT id, status, version FROM control_plane.run WHERE external_id = $1 FOR UPDATE`, externalID).Scan(&runID, &status, &version); err != nil {
		return RunRecord{}, err
	}
	if status == "leased" {
		var existingHash string
		if err := tx.QueryRow(ctx, `SELECT lease_jti_hash FROM control_plane.run_attempt WHERE run_id = $1 AND attempt_no = $2`, runID, attemptNo).Scan(&existingHash); err != nil || existingHash != jtiHash {
			return RunRecord{}, ErrRunVersionConflict
		}
		if err := tx.Commit(ctx); err != nil {
			return RunRecord{}, err
		}
		return r.Get(ctx, externalID)
	}
	if version != expectedVersion || status != "requested" {
		return RunRecord{}, ErrRunVersionConflict
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.run_attempt (id, run_id, attempt_no, runtime_id, lease_jti_hash, status, started_at)
		VALUES ($1, $2, $3, $4, $5, 'leased', now())
	`, uuid.New(), runID, attemptNo, runtimeID, jtiHash); err != nil {
		return RunRecord{}, fmt.Errorf("insert hci_sim run attempt: %w", err)
	}
	record, err := scanRun(tx.QueryRow(ctx, `
		UPDATE control_plane.run SET status = 'leased', version = version + 1, updated_at = now()
		WHERE id = $1 AND version = $2
		RETURNING external_id, support_id, kbd_revision, bundle_digest, variant,
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest
	`, runID, expectedVersion))
	if err != nil {
		return RunRecord{}, ErrRunVersionConflict
	}
	if err := tx.Commit(ctx); err != nil {
		return RunRecord{}, fmt.Errorf("commit hci_sim lease transaction: %w", err)
	}
	return record, nil
}

func (r *RunRepository) getByIdempotency(ctx context.Context, tx pgx.Tx, key string) (RunRecord, error) {
	return scanRun(tx.QueryRow(ctx, `
		SELECT external_id, support_id, kbd_revision, bundle_digest, variant,
		       execution_mode, status, version, deadline_at, idempotency_key, request_digest
		FROM control_plane.run WHERE idempotency_key = $1
	`, key))
}

type runScanner interface {
	Scan(...any) error
}

func scanRun(row runScanner) (RunRecord, error) {
	var record RunRecord
	err := row.Scan(
		&record.ExternalID, &record.SupportID, &record.KBDRevision, &record.BundleDigest,
		&record.Variant, &record.ExecutionMode, &record.Status, &record.Version,
		&record.Deadline, &record.IdempotencyKey, &record.RequestDigest,
	)
	return record, err
}

func validateRunInput(input RunInput) error {
	if input.ExternalID == "" || input.SupportID == "" || input.KBDRevision < 1 || input.Variant == "" || input.BundleDigest == "" || input.ExecutionMode != "sim-ssh" || input.IdempotencyKey == "" || input.RequestDigest == "" || input.InputFingerprint == "" || input.Deadline.IsZero() {
		return errors.New("invalid hci_sim run input")
	}
	return nil
}
