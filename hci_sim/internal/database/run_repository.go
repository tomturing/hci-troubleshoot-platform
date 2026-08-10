package database

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrIdempotencyConflict = errors.New("idempotency_conflict")
	ErrRunVersionConflict  = errors.New("run_version_conflict")
	ErrResultConflict      = errors.New("run_result_conflict")
)

// RunInput 是控制面创建 TestRun 时被冻结的字段集合。Lease 明文、SSH 密码和
// 原始 Artifact 不属于该结构，也不能进入数据库。
type RunInput struct {
	ExternalID         string
	SupportID          string
	KBDRevision        int
	Variant            string
	BundleDigest       string
	ExecutionMode      string
	IdempotencyKey     string
	RequestDigest      string
	Deadline           time.Time
	InputFingerprint   string
	EnvironmentContext map[string]any
}

type RunRecord struct {
	ExternalID         string
	SupportID          string
	KBDRevision        int
	BundleDigest       string
	Variant            string
	ExecutionMode      string
	IdempotencyKey     string
	RequestDigest      string
	Status             string
	Version            int
	Deadline           time.Time
	EnvironmentContext json.RawMessage
}

type OutboxRecord struct {
	ID            int64
	RunExternalID string
	EventType     string
	PayloadDigest string
	Attempts      int
	AvailableAt   time.Time
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
	environmentContext, err := json.Marshal(input.EnvironmentContext)
	if err != nil {
		return RunRecord{}, fmt.Errorf("encode hci_sim environment context: %w", err)
	}

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
			 variant, execution_mode, environment_context, status, idempotency_key, request_digest, deadline_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'requested', $10, $11, $12)
		ON CONFLICT (idempotency_key) DO NOTHING
		RETURNING external_id, support_id, kbd_revision, bundle_digest, variant,
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
	`, runID, input.ExternalID, input.SupportID, input.KBDRevision, scenarioID, input.BundleDigest,
		input.Variant, input.ExecutionMode, environmentContext, input.IdempotencyKey, input.RequestDigest, input.Deadline.UTC())
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
		       execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
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
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
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
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
	`, runID, expectedVersion))
	if err != nil {
		return RunRecord{}, ErrRunVersionConflict
	}
	if err := tx.Commit(ctx); err != nil {
		return RunRecord{}, fmt.Errorf("commit hci_sim lease transaction: %w", err)
	}
	return record, nil
}

// AppendEvent writes an immutable event and its delivery outbox row in one
// transaction. Sequence allocation is serialized by the Run row lock; retries
// with the same payload are idempotent at the outbox boundary.
func (r *RunRepository) AppendEvent(ctx context.Context, externalID string, attemptNo int, eventType, payloadDigest, traceID string) (int, error) {
	if externalID == "" || attemptNo < 1 || eventType == "" || payloadDigest == "" {
		return 0, errors.New("invalid run event")
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, fmt.Errorf("begin hci_sim event transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var runID uuid.UUID
	if err := tx.QueryRow(ctx, `SELECT id FROM control_plane.run WHERE external_id = $1 FOR UPDATE`, externalID).Scan(&runID); err != nil {
		return 0, err
	}
	var seq int
	existingErr := tx.QueryRow(ctx, `
		SELECT seq FROM control_plane.run_event
		WHERE run_id = $1 AND attempt_no = $2 AND event_type = $3 AND payload_digest = $4
		ORDER BY seq LIMIT 1
	`, runID, attemptNo, eventType, payloadDigest).Scan(&seq)
	if existingErr == nil {
		if err := tx.Commit(ctx); err != nil {
			return 0, err
		}
		return seq, nil
	}
	if !errors.Is(existingErr, pgx.ErrNoRows) {
		return 0, existingErr
	}
	if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(seq), 0) + 1 FROM control_plane.run_event WHERE run_id = $1 AND attempt_no = $2`, runID, attemptNo).Scan(&seq); err != nil {
		return 0, fmt.Errorf("allocate hci_sim event sequence: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.run_event (run_id, attempt_no, seq, event_type, payload_digest, trace_id)
		VALUES ($1, $2, $3, $4, $5, NULLIF($6, ''))
	`, runID, attemptNo, seq, eventType, payloadDigest, traceID); err != nil {
		return 0, fmt.Errorf("insert hci_sim run event: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.run_outbox (run_id, event_type, payload_digest)
		VALUES ($1, $2, $3) ON CONFLICT (run_id, event_type, payload_digest) DO NOTHING
	`, runID, eventType, payloadDigest); err != nil {
		return 0, fmt.Errorf("enqueue hci_sim run event: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit hci_sim event transaction: %w", err)
	}
	return seq, nil
}

// RecordResult closes an Attempt and advances its Run atomically. A repeated
// identical result is accepted; a conflicting result is rejected.
func (r *RunRepository) RecordResult(ctx context.Context, externalID string, attemptNo int, oracleVersion, outcome, reportURI, reportDigest string) (RunRecord, error) {
	if externalID == "" || attemptNo < 1 || oracleVersion == "" || reportURI == "" || reportDigest == "" {
		return RunRecord{}, errors.New("invalid run result")
	}
	status := outcomeToRunStatus(outcome)
	if status == "" || !strings.HasPrefix(reportURI, "object://") || strings.Contains(strings.ToLower(reportURI), "password") || strings.Contains(strings.ToLower(reportURI), "token") {
		return RunRecord{}, errors.New("invalid or sensitive run result")
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return RunRecord{}, fmt.Errorf("begin hci_sim result transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var runID uuid.UUID
	var currentStatus string
	var version int
	if err := tx.QueryRow(ctx, `SELECT id, status, version FROM control_plane.run WHERE external_id = $1 FOR UPDATE`, externalID).Scan(&runID, &currentStatus, &version); err != nil {
		return RunRecord{}, err
	}
	var existingOutcome, existingDigest string
	existingErr := tx.QueryRow(ctx, `SELECT outcome, report_digest FROM control_plane.run_result WHERE run_id = $1 AND attempt_no = $2`, runID, attemptNo).Scan(&existingOutcome, &existingDigest)
	if existingErr == nil {
		if existingOutcome != outcome || existingDigest != reportDigest {
			return RunRecord{}, ErrResultConflict
		}
		if err := tx.Commit(ctx); err != nil {
			return RunRecord{}, err
		}
		return r.Get(ctx, externalID)
	}
	if !errors.Is(existingErr, pgx.ErrNoRows) {
		return RunRecord{}, existingErr
	}
	var attemptExists bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM control_plane.run_attempt WHERE run_id = $1 AND attempt_no = $2)`, runID, attemptNo).Scan(&attemptExists); err != nil || !attemptExists {
		return RunRecord{}, errors.New("run_attempt_not_found")
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.run_result (run_id, attempt_no, oracle_version, outcome, report_uri, report_digest)
		VALUES ($1, $2, $3, $4, $5, $6)
	`, runID, attemptNo, oracleVersion, outcome, reportURI, reportDigest); err != nil {
		return RunRecord{}, fmt.Errorf("insert hci_sim run result: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE control_plane.run_attempt SET status = $1, ended_at = now()
		WHERE run_id = $2 AND attempt_no = $3
	`, status, runID, attemptNo); err != nil {
		return RunRecord{}, fmt.Errorf("close hci_sim run attempt: %w", err)
	}
	record, err := scanRun(tx.QueryRow(ctx, `
		UPDATE control_plane.run SET status = $1, version = version + 1, updated_at = now()
		WHERE id = $2 AND status NOT IN ('passed', 'failed', 'inconclusive', 'cancelled', 'expired')
		RETURNING external_id, support_id, kbd_revision, bundle_digest, variant,
		          execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
	`, status, runID))
	if errors.Is(err, pgx.ErrNoRows) {
		if currentStatus != status {
			return RunRecord{}, ErrRunVersionConflict
		}
		record, err = scanRun(tx.QueryRow(ctx, `
			SELECT external_id, support_id, kbd_revision, bundle_digest, variant,
			       execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
			FROM control_plane.run WHERE id = $1
		`, runID))
	}
	if err != nil {
		return RunRecord{}, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO control_plane.run_outbox (run_id, event_type, payload_digest)
		VALUES ($1, 'run.result', $2) ON CONFLICT (run_id, event_type, payload_digest) DO NOTHING
	`, runID, reportDigest); err != nil {
		return RunRecord{}, fmt.Errorf("enqueue hci_sim result: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return RunRecord{}, fmt.Errorf("commit hci_sim result transaction: %w", err)
	}
	return record, nil
}

// ClaimOutbox leases one pending delivery without blocking another reconciler.
func (r *RunRepository) ClaimOutbox(ctx context.Context) (OutboxRecord, error) {
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return OutboxRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var record OutboxRecord
	var runID uuid.UUID
	row := tx.QueryRow(ctx, `
		SELECT o.id, r.external_id, o.event_type, o.payload_digest, o.attempts, o.available_at, o.run_id
		FROM control_plane.run_outbox o JOIN control_plane.run r ON r.id = o.run_id
		WHERE o.status = 'pending' AND o.available_at <= now()
		ORDER BY o.id FOR UPDATE SKIP LOCKED LIMIT 1
	`)
	if err := row.Scan(&record.ID, &record.RunExternalID, &record.EventType, &record.PayloadDigest, &record.Attempts, &record.AvailableAt, &runID); err != nil {
		return OutboxRecord{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE control_plane.run_outbox SET status = 'processing', attempts = attempts + 1, processing_at = now() WHERE id = $1`, record.ID); err != nil {
		return OutboxRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return OutboxRecord{}, err
	}
	record.Attempts++
	return record, nil
}

func (r *RunRepository) CompleteOutbox(ctx context.Context, id int64, success bool, retryAt time.Time) error {
	if id < 1 {
		return errors.New("invalid outbox id")
	}
	status := "pending"
	if success {
		status = "processed"
	}
	_, err := r.pool.Exec(ctx, `
		UPDATE control_plane.run_outbox
		SET status = $1, available_at = CASE WHEN $2 THEN available_at ELSE $3 END,
		    processing_at = NULL, processed_at = CASE WHEN $2 THEN now() ELSE NULL END
		WHERE id = $4 AND status = 'processing'
	`, status, success, retryAt.UTC(), id)
	return err
}

// RecoverProcessingOutbox returns abandoned processing records to pending. A
// reconciler crash must not permanently strand an event; records over the
// attempt budget are moved to failed for operator review.
func (r *RunRepository) RecoverProcessingOutbox(ctx context.Context, olderThan time.Time, maxAttempts int) (int64, error) {
	if maxAttempts < 1 {
		return 0, errors.New("invalid outbox attempt budget")
	}
	result, err := r.pool.Exec(ctx, `
		UPDATE control_plane.run_outbox
		SET status = CASE WHEN attempts >= $2 THEN 'failed' ELSE 'pending' END,
		    available_at = now(), processing_at = NULL
		WHERE status = 'processing' AND COALESCE(processing_at, created_at) < $1
	`, olderThan.UTC(), maxAttempts)
	return result.RowsAffected(), err
}

// ExpireRuns is the restart/deadline safety net. It only touches non-terminal
// runs and emits an outbox notification in the same transaction.
func (r *RunRepository) ExpireRuns(ctx context.Context, now time.Time) (int64, error) {
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `
		UPDATE control_plane.run
		SET status = 'expired', version = version + 1, updated_at = now()
		WHERE deadline_at <= $1 AND status IN ('requested', 'preparing', 'leased', 'running')
		RETURNING id, external_id
	`, now.UTC())
	if err != nil {
		return 0, err
	}
	type expiredRun struct {
		id         uuid.UUID
		externalID string
	}
	expired := make([]expiredRun, 0)
	for rows.Next() {
		var item expiredRun
		if err := rows.Scan(&item.id, &item.externalID); err != nil {
			rows.Close()
			return 0, err
		}
		expired = append(expired, item)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()
	for _, item := range expired {
		digest := textDigest("run.expired:" + item.externalID)
		if _, err := tx.Exec(ctx, `
			INSERT INTO control_plane.run_outbox (run_id, event_type, payload_digest)
			VALUES ($1, 'run.expired', $2) ON CONFLICT DO NOTHING
		`, item.id, digest); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return int64(len(expired)), nil
}

func (r *RunRepository) getByIdempotency(ctx context.Context, tx pgx.Tx, key string) (RunRecord, error) {
	return scanRun(tx.QueryRow(ctx, `
		SELECT external_id, support_id, kbd_revision, bundle_digest, variant,
		       execution_mode, status, version, deadline_at, idempotency_key, request_digest, environment_context
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
		&record.EnvironmentContext,
	)
	return record, err
}

func validateRunInput(input RunInput) error {
	if input.ExternalID == "" || input.SupportID == "" || input.KBDRevision < 1 || input.Variant == "" || input.BundleDigest == "" || input.ExecutionMode != "sim-ssh" || input.IdempotencyKey == "" || input.RequestDigest == "" || input.InputFingerprint == "" || input.Deadline.IsZero() || input.EnvironmentContext == nil {
		return errors.New("invalid hci_sim run input")
	}
	return nil
}

func outcomeToRunStatus(outcome string) string {
	switch outcome {
	case "passed", "positive":
		return "passed"
	case "failed", "negative":
		return "failed"
	case "inconclusive":
		return "inconclusive"
	default:
		return ""
	}
}

func textDigest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return fmt.Sprintf("sha256:%x", sum[:])
}
