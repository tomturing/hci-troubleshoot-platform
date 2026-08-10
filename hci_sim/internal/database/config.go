// Package database contains the Runtime's fail-closed database boundary.
//
// The control plane and the SSH data plane are separate concerns, but the
// Runtime must still refuse to start with the platform database by accident.
// This package deliberately validates the target before a future persistent
// Store is injected; it does not silently fall back to DATABASE_URL.
package database

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
)

const requiredDatabaseName = "hci_sim"

type Target struct {
	URL        string
	Database   string
	Configured bool
}

func FromEnvironment() (Target, error) {
	raw := strings.TrimSpace(os.Getenv("HCI_SIM_DATABASE_URL"))
	required := strings.EqualFold(strings.TrimSpace(os.Getenv("HCI_SIM_DATABASE_REQUIRED")), "true")
	if raw == "" {
		if required {
			return Target{}, errors.New("HCI_SIM_DATABASE_URL is required when HCI_SIM_DATABASE_REQUIRED=true")
		}
		return Target{Configured: false}, nil
	}
	target, err := Parse(raw)
	if err != nil {
		return Target{}, err
	}
	return target, nil
}

func Parse(raw string) (Target, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return Target{}, fmt.Errorf("invalid HCI_SIM_DATABASE_URL: %w", err)
	}
	if parsed.Scheme != "postgres" && parsed.Scheme != "postgresql" {
		return Target{}, fmt.Errorf("invalid HCI_SIM_DATABASE_URL: unsupported scheme %q", parsed.Scheme)
	}
	if parsed.Host == "" {
		return Target{}, errors.New("invalid HCI_SIM_DATABASE_URL: host is required")
	}
	databaseName := strings.TrimPrefix(parsed.Path, "/")
	if databaseName != requiredDatabaseName {
		return Target{}, fmt.Errorf("hci-sim Runtime must target database %q, got %q", requiredDatabaseName, databaseName)
	}
	return Target{URL: raw, Database: databaseName, Configured: true}, nil
}
