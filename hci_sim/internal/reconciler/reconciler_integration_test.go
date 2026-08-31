package reconciler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"hci_sim/internal/database"

	"github.com/jackc/pgx/v5"
)

func TestReconcilePostgresNoFalseSuccessAndWebhookAck(t *testing.T) {
	rawURL := os.Getenv("HCI_SIM_TEST_DATABASE_URL")
	if rawURL == "" {
		t.Skip("HCI_SIM_TEST_DATABASE_URL is not configured")
	}
	target, err := database.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, target)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	repository, err := database.NewRunRepository(pool)
	if err != nil {
		t.Fatal(err)
	}
	// database 包的 Bundle 编译集成测试会写统一 outbox；本用例只验证下方新建 Run，
	// 先完成其它 topic，避免共享临时库时把合法 bundle 事件误判为 no.sink 事件。
	for {
		record, claimErr := repository.ClaimOutbox(ctx)
		if errors.Is(claimErr, pgx.ErrNoRows) {
			break
		}
		if claimErr != nil {
			t.Fatal(claimErr)
		}
		if err := repository.CompleteOutbox(ctx, record.ID, true, time.Time{}); err != nil {
			t.Fatal(err)
		}
	}
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	input := database.RunInput{
		ExternalID: "run-reconcile-" + suffix, SupportID: "27123", KBDRevision: 1,
		Variant: "positive-realistic", BundleDigest: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		BundleSchemaVersion: "2.0", BundleObjectURI: "embedded://test/reconcile-fixture.json",
		BundleObjectDigest: "sha256:9999999999999999999999999999999999999999999999999999999999999999", BundleSizeBytes: 1024,
		ExecutionMode: "sim-ssh", IdempotencyKey: "reconcile-" + suffix,
		RequestDigest: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		Deadline:      time.Now().UTC().Add(time.Hour), InputFingerprint: "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		EnvironmentContext: map[string]any{"test_run_id": "run-reconcile-" + suffix, "support_id": "27123", "kbd_revision": 1, "bundle_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "execution_mode": "sim-ssh"},
	}
	created, err := repository.Create(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := repository.RecordLease(ctx, input.ExternalID, created.Version, 1, "runtime-test", "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"); err != nil {
		t.Fatal(err)
	}
	if _, err := repository.AppendEvent(ctx, input.ExternalID, 1, "no.sink", "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", ""); err != nil {
		t.Fatal(err)
	}
	if err := ReconcileOnce(ctx, repository, Config{}); err != nil {
		t.Fatal(err)
	}
	var pending database.OutboxRecord
	for {
		pending, err = repository.ClaimOutbox(ctx)
		if err != nil {
			t.Fatalf("claim no.sink outbox: %v", err)
		}
		if pending.RunExternalID == input.ExternalID && pending.EventType == "no.sink" {
			break
		}
		if err := repository.CompleteOutbox(ctx, pending.ID, true, time.Time{}); err != nil {
			t.Fatalf("complete unrelated outbox: %v", err)
		}
	}
	if err := repository.CompleteOutbox(ctx, pending.ID, true, time.Time{}); err != nil {
		t.Fatal(err)
	}

	if _, err := repository.AppendEvent(ctx, input.ExternalID, 1, "with.sink", "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", ""); err != nil {
		t.Fatal(err)
	}
	var delivered map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer r.Body.Close()
		_ = json.NewDecoder(r.Body).Decode(&delivered)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	for attempt := 0; attempt < 32; attempt++ {
		delivered = nil
		if err := ReconcileOnce(ctx, repository, Config{WebhookURL: server.URL, MaxAttempts: 3}); err != nil {
			t.Fatal(err)
		}
		if delivered["run_external_id"] == input.ExternalID && delivered["event_type"] == "with.sink" {
			break
		}
	}
	if delivered["run_external_id"] != input.ExternalID || delivered["event_type"] != "with.sink" {
		t.Fatalf("unexpected webhook payload: %+v", delivered)
	}
	var ownPending int
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM control_plane.outbox
		WHERE topic = 'run' AND aggregate_id = $1 AND status <> 'processed'
	`, input.ExternalID).Scan(&ownPending); err != nil || ownPending != 0 {
		t.Fatalf("processed run outbox remained pending: count=%d err=%v", ownPending, err)
	}
}
