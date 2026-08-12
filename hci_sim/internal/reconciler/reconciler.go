// Package reconciler provides the Runtime-side delivery loop for durable
// hci_sim outbox records. It never marks an item processed without a 2xx
// acknowledgement from the configured control-plane sink.
package reconciler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"hci_sim/internal/database"

	"github.com/jackc/pgx/v5"
)

type Config struct {
	WebhookURL  string
	Interval    time.Duration
	MaxAttempts int
	Client      *http.Client
}

func Run(ctx context.Context, repository *database.RunRepository, config Config) {
	if repository == nil {
		return
	}
	if config.Interval <= 0 {
		config.Interval = 2 * time.Second
	}
	if config.MaxAttempts < 1 {
		config.MaxAttempts = 8
	}
	if config.Client == nil {
		config.Client = &http.Client{Timeout: 5 * time.Second}
	}
	ticker := time.NewTicker(config.Interval)
	defer ticker.Stop()
	for {
		_ = ReconcileOnce(ctx, repository, config)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func ReconcileOnce(ctx context.Context, repository *database.RunRepository, config Config) error {
	if repository == nil {
		return errors.New("hci_sim repository is required")
	}
	if config.MaxAttempts < 1 {
		config.MaxAttempts = 8
	}
	if config.Client == nil {
		config.Client = &http.Client{Timeout: 5 * time.Second}
	}
	now := time.Now().UTC()
	_, _ = repository.RecoverProcessingOutbox(ctx, now.Add(-time.Minute), config.MaxAttempts)
	_, _ = repository.ExpireRuns(ctx, now)
	if config.WebhookURL == "" {
		// No sink means no false success. Records remain pending and are visible
		// to operators until the control-plane delivery endpoint is configured.
		return nil
	}
	for i := 0; i < 32; i++ {
		record, err := repository.ClaimOutbox(ctx)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		payload, _ := json.Marshal(map[string]any{
			"run_external_id": record.RunExternalID,
			"event_type":      record.EventType,
			"payload_digest":  record.PayloadDigest,
			"attempts":        record.Attempts,
		})
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, config.WebhookURL, bytes.NewReader(payload))
		if err != nil {
			return err
		}
		request.Header.Set("Content-Type", "application/json")
		response, err := config.Client.Do(request)
		if err == nil {
			_ = response.Body.Close()
		}
		success := err == nil && response.StatusCode >= 200 && response.StatusCode < 300
		retryAt := time.Now().UTC().Add(time.Duration(record.Attempts) * time.Second)
		if completeErr := repository.CompleteOutbox(ctx, record.ID, success, retryAt); completeErr != nil {
			return completeErr
		}
	}
	return nil
}
