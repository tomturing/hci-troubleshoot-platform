package database

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"
)

func TestRunRepositoryPostgresIdempotencyAndCAS(t *testing.T) {
	rawURL := os.Getenv("HCI_SIM_TEST_DATABASE_URL")
	if rawURL == "" {
		t.Skip("HCI_SIM_TEST_DATABASE_URL is not configured")
	}
	target, err := Parse(rawURL)
	if err != nil {
		t.Fatalf("parse test database URL: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := Open(ctx, target)
	if err != nil {
		t.Fatalf("open test database: %v", err)
	}
	defer pool.Close()
	repository, err := NewRunRepository(pool)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	suffix := fmt.Sprintf("%d", now.UnixNano())
	input := RunInput{
		ExternalID:         "run-integration-" + suffix,
		SupportID:          "27123",
		KBDRevision:        1,
		Variant:            "positive-realistic",
		BundleDigest:       "sha256:d7ddfb849cb1a3d0879af2a6887b0c77506632bfa7c8261cf656305d06e30bfa",
		ExecutionMode:      "sim-ssh",
		IdempotencyKey:     "integration-idempotency-" + suffix,
		RequestDigest:      "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		Deadline:           now.Add(time.Hour),
		InputFingerprint:   "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		EnvironmentContext: map[string]any{"test_run_id": "run-integration-" + suffix, "support_id": "27123", "kbd_revision": 1, "bundle_digest": "sha256:d7ddfb849cb1a3d0879af2a6887b0c77506632bfa7c8261cf656305d06e30bfa", "execution_mode": "sim-ssh"},
	}
	created, err := repository.Create(ctx, input)
	if err != nil {
		t.Fatalf("create run: %v", err)
	}
	if created.ExternalID != input.ExternalID || created.Status != "requested" || created.Version != 1 {
		t.Fatalf("unexpected created run: %+v", created)
	}
	replayed, err := repository.Create(ctx, input)
	if err != nil {
		t.Fatalf("idempotent replay: %v", err)
	}
	if replayed.ExternalID != created.ExternalID || replayed.RequestDigest != created.RequestDigest {
		t.Fatalf("idempotent replay changed record: %+v", replayed)
	}
	conflict := input
	conflict.RequestDigest = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	if _, err := repository.Create(ctx, conflict); err == nil || err.Error() != "idempotency_conflict" {
		t.Fatalf("expected idempotency_conflict, got %v", err)
	}
	running, err := repository.UpdateStatusCAS(ctx, input.ExternalID, created.Version, "running")
	if err != nil {
		t.Fatalf("status CAS: %v", err)
	}
	if running.Status != "running" || running.Version != 2 {
		t.Fatalf("unexpected CAS result: %+v", running)
	}
	if _, err := repository.UpdateStatusCAS(ctx, input.ExternalID, 1, "passed"); err == nil || err.Error() != "run_version_conflict" {
		t.Fatalf("expected stale CAS conflict, got %v", err)
	}
	leasedInput := input
	leasedInput.ExternalID = input.ExternalID + "-lease"
	leasedInput.IdempotencyKey = input.IdempotencyKey + "-lease"
	leasedInput.InputFingerprint = input.InputFingerprint[:len(input.InputFingerprint)-1] + "c"
	createdLeaseRun, err := repository.Create(ctx, leasedInput)
	if err != nil {
		t.Fatalf("create lease run: %v", err)
	}
	leased, err := repository.RecordLease(ctx, leasedInput.ExternalID, createdLeaseRun.Version, 1, "runtime-test", "sha256:lease-jti-hash")
	if err != nil {
		t.Fatalf("record lease: %v", err)
	}
	if leased.Status != "leased" || leased.Version != createdLeaseRun.Version+1 {
		t.Fatalf("unexpected leased run: %+v", leased)
	}
	replayedLease, err := repository.RecordLease(ctx, leasedInput.ExternalID, leased.Version, 1, "runtime-test", "sha256:lease-jti-hash")
	if err != nil || replayedLease.Status != "leased" {
		t.Fatalf("idempotent lease replay failed: %+v %v", replayedLease, err)
	}
}

func TestRunRepositoryPostgresEventResultAndOutbox(t *testing.T) {
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
	repository, err := NewRunRepository(pool)
	if err != nil {
		t.Fatal(err)
	}
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	input := RunInput{
		ExternalID: "run-event-" + suffix, SupportID: "27123", KBDRevision: 1,
		Variant: "positive-realistic", BundleDigest: "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
		ExecutionMode: "sim-ssh", IdempotencyKey: "event-idempotency-" + suffix,
		RequestDigest: "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
		Deadline:      time.Now().UTC().Add(time.Hour), InputFingerprint: "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
		EnvironmentContext: map[string]any{"test_run_id": "run-event-" + suffix, "support_id": "27123", "kbd_revision": 1, "bundle_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "execution_mode": "sim-ssh"},
	}
	created, err := repository.Create(ctx, input)
	if err != nil {
		t.Fatalf("create event run: %v", err)
	}
	leased, err := repository.RecordLease(ctx, input.ExternalID, created.Version, 1, "runtime-test", "sha256:1111111111111111111111111111111111111111111111111111111111111111")
	if err != nil {
		t.Fatalf("lease event run: %v", err)
	}
	seq, err := repository.AppendEvent(ctx, input.ExternalID, 1, "exec.done", "sha256:2222222222222222222222222222222222222222222222222222222222222222", "trace-test")
	if err != nil || seq != 1 {
		t.Fatalf("append event: seq=%d err=%v", seq, err)
	}
	replayedSeq, err := repository.AppendEvent(ctx, input.ExternalID, 1, "exec.done", "sha256:2222222222222222222222222222222222222222222222222222222222222222", "trace-test")
	if err != nil || replayedSeq != seq {
		t.Fatalf("event replay is not idempotent: seq=%d err=%v", replayedSeq, err)
	}
	passed, err := repository.RecordResult(ctx, input.ExternalID, 1, "oracle-v1", "passed", "object://reports/"+input.ExternalID, "sha256:3333333333333333333333333333333333333333333333333333333333333333")
	if err != nil || passed.Status != "passed" || passed.Version != leased.Version+1 {
		t.Fatalf("record result: %+v err=%v", passed, err)
	}
	replayedResult, err := repository.RecordResult(ctx, input.ExternalID, 1, "oracle-v1", "passed", "object://reports/"+input.ExternalID, "sha256:3333333333333333333333333333333333333333333333333333333333333333")
	if err != nil || replayedResult.Status != "passed" {
		t.Fatalf("result replay is not idempotent: %+v err=%v", replayedResult, err)
	}
	if _, err := repository.RecordResult(ctx, input.ExternalID, 1, "oracle-v1", "failed", "object://reports/"+input.ExternalID, "sha256:4444444444444444444444444444444444444444444444444444444444444444"); !errors.Is(err, ErrResultConflict) {
		t.Fatalf("expected conflicting result rejection, got %v", err)
	}
	claimed, err := repository.ClaimOutbox(ctx)
	if err != nil {
		t.Fatalf("claim outbox: %v", err)
	}
	if claimed.RunExternalID != input.ExternalID || claimed.Attempts != 1 {
		t.Fatalf("unexpected outbox claim: %+v", claimed)
	}
	if err := repository.CompleteOutbox(ctx, claimed.ID, true, time.Time{}); err != nil {
		t.Fatalf("complete outbox: %v", err)
	}
	claimedRetry, err := repository.ClaimOutbox(ctx)
	if err != nil {
		t.Fatalf("claim retry outbox: %v", err)
	}
	if err := repository.CompleteOutbox(ctx, claimedRetry.ID, false, time.Now().UTC()); err != nil {
		t.Fatalf("requeue outbox: %v", err)
	}
	claimedStuck, err := repository.ClaimOutbox(ctx)
	if err != nil {
		t.Fatalf("claim stuck outbox: %v", err)
	}
	if recovered, err := repository.RecoverProcessingOutbox(ctx, time.Now().UTC().Add(time.Second), 8); err != nil || recovered != 1 {
		t.Fatalf("recover processing outbox: count=%d err=%v", recovered, err)
	}
	recoveredClaim, err := repository.ClaimOutbox(ctx)
	if err != nil || recoveredClaim.ID != claimedStuck.ID {
		t.Fatalf("reclaim recovered outbox: %+v %v", recoveredClaim, err)
	}
	if err := repository.CompleteOutbox(ctx, recoveredClaim.ID, true, time.Time{}); err != nil {
		t.Fatalf("complete recovered outbox: %v", err)
	}
}
