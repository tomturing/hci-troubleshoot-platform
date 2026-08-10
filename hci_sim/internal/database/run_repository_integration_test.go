package database

import (
	"context"
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
		ExternalID:       "run-integration-" + suffix,
		SupportID:        "27123",
		KBDRevision:      1,
		Variant:          "positive-realistic",
		BundleDigest:     "sha256:d7ddfb849cb1a3d0879af2a6887b0c77506632bfa7c8261cf656305d06e30bfa",
		ExecutionMode:    "sim-ssh",
		IdempotencyKey:   "integration-idempotency-" + suffix,
		RequestDigest:    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		Deadline:         now.Add(time.Hour),
		InputFingerprint: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
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
