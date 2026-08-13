package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"hci_sim/internal/database"
	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
	"hci_sim/internal/metrics"
	"hci_sim/internal/reconciler"
	"hci_sim/internal/server"
	"hci_sim/internal/telemetry"

	"golang.org/x/crypto/ssh"
)

var version = "dev"

func main() {
	log.SetFlags(0)
	if filepath.Base(os.Args[0]) == "acli" {
		os.Exit(runLocalACLI(os.Args[1:]))
	}
	if len(os.Args) > 1 && os.Args[1] == "lease" {
		if err := runLease(os.Args[2:]); err != nil {
			log.Fatal(err)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "manifest-digest" {
		if err := runManifestDigest(os.Args[2:]); err != nil {
			log.Fatal(err)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "bootstrap" {
		if err := runBootstrap(os.Args[2:]); err != nil {
			log.Fatal(err)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "offline-manifest" {
		if err := runOfflineManifest(os.Args[2:]); err != nil {
			log.Fatal(err)
		}
		return
	}
	if err := runServer(); err != nil {
		log.Fatal(err)
	}
}

func runLocalACLI(args []string) int {
	router, err := fixture.Load(env("HCI_SIM_FIXTURE_MANIFEST", "/etc/hci-sim/fixture-manifest.json"))
	if err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim local adapter:", err)
		return 126
	}
	argv := append([]string{"acli"}, args...)
	result, err := router.MatchArgv(
		argv,
		env("HCI_SIM_FIXTURE_VARIANT", "positive"),
		env("HCI_SIM_VIRTUAL_NODE_ID", "SIM-HCI-NODE-01"),
		env("HCI_SIM_CONTAINER", "host"),
	)
	if err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim local adapter:", err)
		return 127
	}
	exitCode := result.ExitCode
	stdoutValue, stderrValue := result.Stdout, result.Stderr
	switch result.Fault.Type {
	case fixture.FaultTimeout:
		exitCode = 124
		stderrValue = "hci-sim: synthetic timeout\n"
	case fixture.FaultPermission:
		if exitCode == 0 {
			exitCode = 13
		}
	case fixture.FaultNonzeroExit:
		if exitCode == 0 {
			exitCode = 1
		}
	case fixture.FaultTruncate:
		if result.Fault.MaxBytes > 0 {
			stdoutValue = truncateString(stdoutValue, result.Fault.MaxBytes)
			remaining := result.Fault.MaxBytes - len(stdoutValue)
			if remaining < 0 {
				remaining = 0
			}
			stderrValue = truncateString(stderrValue, remaining)
		}
	case fixture.FaultDisconnect:
		exitCode = 255
		stdoutValue = ""
		stderrValue = "hci-sim: synthetic disconnect\n"
	}
	_, _ = os.Stdout.WriteString(stdoutValue)
	_, _ = os.Stderr.WriteString(stderrValue)
	appendLocalAudit(router, result, exitCode, stdoutValue, stderrValue)
	return exitCode
}

func truncateString(value string, size int) string {
	if size < 1 {
		return ""
	}
	if len(value) <= size {
		return value
	}
	return value[:size]
}

func appendLocalAudit(router *fixture.Router, result fixture.Result, exitCode int, stdoutValue, stderrValue string) {
	path := strings.TrimSpace(os.Getenv("HCI_SIM_AUDIT_FILE"))
	if path == "" {
		return
	}
	row := map[string]any{
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "event": "local.exec.done",
		"lab_run_id": os.Getenv("HCI_SIM_LAB_RUN_ID"), "support_id": router.KBD().SupportID,
		"kbd_revision": router.KBD().Revision, "bundle_digest": router.BundleDigest(),
		"variant": os.Getenv("HCI_SIM_FIXTURE_VARIANT"), "fixture_id": result.FixtureID,
		"signal_id": result.SignalID, "command_fingerprint": result.CommandFingerprint,
		"exit_code": exitCode, "stdout_bytes": len(stdoutValue), "stderr_bytes": len(stderrValue),
		"stdout_sha256": digestValue(stdoutValue), "stderr_sha256": digestValue(stderrValue),
	}
	raw, err := json.Marshal(row)
	if err != nil {
		return
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer file.Close()
	_, _ = file.Write(append(raw, '\n'))
}

func runServer() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	databaseTarget, err := database.FromEnvironment()
	if err != nil {
		return err
	}
	var runRepository *database.RunRepository
	var databasePool interface{ Close() }
	if databaseTarget.Configured {
		pool, openErr := database.Open(ctx, databaseTarget)
		if openErr != nil {
			return openErr
		}
		databasePool = pool
		defer databasePool.Close()
		runRepository, err = database.NewRunRepository(pool)
		if err != nil {
			return err
		}
	}
	var eventRecorder server.EventRecorder
	if runRepository != nil {
		eventRecorder = repositoryEventRecorder{repository: runRepository}
	}
	router, err := fixture.Load(env("HCI_SIM_FIXTURE_MANIFEST", "/etc/hci-sim/fixture-manifest.json"))
	if err != nil {
		return err
	}
	secret := []byte(strings.TrimSpace(os.Getenv("HCI_SIM_LEASE_HMAC_KEY")))
	if len(secret) < 32 {
		return errors.New("HCI_SIM_LEASE_HMAC_KEY 至少需要 32 字节")
	}
	signer, err := loadHostSigner(env("HCI_SIM_HOST_KEY_FILE", "/etc/hci-sim-secrets/ssh_host_key"))
	if err != nil {
		return err
	}
	shutdownTrace, err := telemetry.Init(ctx)
	if err != nil {
		return fmt.Errorf("初始化 OpenTelemetry 失败: %w", err)
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = shutdownTrace(shutdownCtx)
	}()
	prom := &metrics.Metrics{}
	sshServer, err := server.New(server.Config{
		ListenAddress: env("HCI_SIM_SSH_LISTEN", ":2222"), HostSigner: signer,
		LeaseSecret: secret, Router: router, Workers: envInt("HCI_SIM_WORKERS", 8),
		QueueSize: envInt("HCI_SIM_QUEUE_SIZE", 128), MaxOutputBytes: envInt("HCI_SIM_MAX_OUTPUT_BYTES", router.OutputLimit()),
		LeaseIssuer: env("HCI_SIM_LEASE_ISSUER", "hci-platform"), LeaseAudience: env("HCI_SIM_LEASE_AUDIENCE", "hci-sim"), Metrics: prom, Recorder: eventRecorder,
	})
	if err != nil {
		return err
	}
	httpServer := &http.Server{Addr: env("HCI_SIM_HTTP_LISTEN", ":8080"), ReadHeaderTimeout: 5 * time.Second}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) })
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if runRepository != nil {
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			err := runRepository.Ping(ctx)
			cancel()
			if err != nil {
				http.Error(w, "hci_sim persistence unavailable", http.StatusServiceUnavailable)
				return
			}
		}
		_, _ = w.Write([]byte("ready\n"))
	})
	mux.Handle("/metrics", prom.Handler())
	mux.HandleFunc("/status", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"service": "hci-sim", "version": version, "fixture_manifest_hash": router.ManifestHash(), "bundle_digest": router.BundleDigest(),
			"kbd_support_id": router.KBD().SupportID, "kbd_revision": router.KBD().Revision, "tool_contract_revision": router.Contracts().ToolRevision,
			"database_configured": databaseTarget.Configured, "database_name": databaseTarget.Database,
			"outbox_sink_configured": strings.TrimSpace(os.Getenv("HCI_SIM_OUTBOX_WEBHOOK_URL")) != "",
		})
	})
	// 控制面最小 HTTP 契约。生产入口必须由 API Gateway/NetworkPolicy 保护，
	// Runtime 只接受 immutable fixture，并且永不回退真实 HCI。
	fixtureVariant := simulationFixtureVariant()
	controlToken := strings.TrimSpace(os.Getenv("HCI_SIM_CONTROL_TOKEN"))
	allowInsecureControlAPI := strings.EqualFold(strings.TrimSpace(os.Getenv("HCI_SIM_ALLOW_INSECURE_CONTROL_API")), "true")
	controlAuthorized := func(r *http.Request) bool {
		return (controlToken != "" && r.Header.Get("Authorization") == "Bearer "+controlToken) || (controlToken == "" && allowInsecureControlAPI)
	}
	mux.HandleFunc("/v1/simulations/capabilities/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		requestedID := strings.TrimSpace(strings.TrimPrefix(r.URL.Path, "/v1/simulations/capabilities/"))
		if requestedID == "" || strings.ContainsAny(requestedID, "/?#") {
			http.Error(w, "kbd_id is required", http.StatusBadRequest)
			return
		}
		runtimeKBD := router.KBD()
		runtimeRevision := runtimeKBD.Revision
		authorityScope := env("HCI_SIM_AUTHORITY_SCOPE", "runtime_fixture")
		activeRevision := envInt("HCI_SIM_ACTIVE_REVISION", runtimeRevision)
		buildable := simulationBuildable(requestedID, runtimeKBD, activeRevision, router.BundleDigest(), authorityScope, router.IsSynthetic())
		gaps := make([]string, 0, 3)
		if requestedID != runtimeKBD.SupportID {
			gaps = append(gaps, "kbd_not_loaded")
		}
		if activeRevision != runtimeRevision {
			gaps = append(gaps, "kbd_revision_mismatch")
		}
		if router.BundleDigest() == "" {
			gaps = append(gaps, "bundle_digest_missing")
		}
		if router.IsSynthetic() {
			gaps = append(gaps, "synthetic_fixture")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"support_id": requestedID, "requested_revision": activeRevision, "runtime_revision": runtimeRevision,
			"bundle_digest": router.BundleDigest(), "bundle_status": "published", "authority_scope": authorityScope,
			"synthetic": router.IsSynthetic(), "buildable": buildable, "capability_gap": gaps,
		})
	})
	mux.HandleFunc("/v1/simulations/build", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var request struct {
			KBDID  string `json:"kbd_id"`
			CaseID string `json:"case_id"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&request); err != nil || strings.TrimSpace(request.KBDID) == "" {
			http.Error(w, "kbd_id is required", http.StatusBadRequest)
			return
		}
		if router.KBD().SupportID != strings.TrimSpace(request.KBDID) {
			http.Error(w, "capability_gap: requested KBD is not the loaded immutable fixture", http.StatusConflict)
			return
		}
		if env("HCI_SIM_AUTHORITY_SCOPE", "runtime_fixture") == "runtime_fixture" || router.IsSynthetic() {
			http.Error(w, "capability_gap: Runtime fixture lacks an explicit non-synthetic authority scope", http.StatusConflict)
			return
		}
		now := time.Now().UTC()
		runID := fmt.Sprintf("run-%s-%s", request.KBDID, randomID())
		idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
		if idempotencyKey == "" {
			idempotencyKey = "runtime-" + runID
		}
		if len(idempotencyKey) > 256 {
			http.Error(w, "Idempotency-Key is too long", http.StatusBadRequest)
			return
		}
		requestDigest := digestValue(map[string]any{"kbd_id": request.KBDID, "variant": fixtureVariant, "bundle_digest": router.BundleDigest()})
		inputFingerprint := digestValue(map[string]any{"support_id": request.KBDID, "kbd_revision": router.KBD().Revision, "variant": fixtureVariant, "bundle_digest": router.BundleDigest()})
		environmentContext := simulationEnvironmentContext(router, runID, strings.TrimSpace(request.CaseID), fixtureVariant)
		runVersion := 1
		if runRepository != nil {
			record, persistErr := runRepository.Create(r.Context(), database.RunInput{
				ExternalID: runID, SupportID: request.KBDID, KBDRevision: router.KBD().Revision,
				Variant: fixtureVariant, BundleDigest: router.BundleDigest(),
				BundleSchemaVersion: router.SchemaVersion(), BundleObjectURI: "embedded://hci-sim/fixture-manifest.json",
				BundleObjectDigest: router.ManifestHash(), BundleSizeBytes: router.ManifestSize(), ExecutionMode: "sim-ssh",
				IdempotencyKey: idempotencyKey, RequestDigest: requestDigest, Deadline: now.Add(15 * time.Minute),
				InputFingerprint:   inputFingerprint,
				EnvironmentContext: environmentContext,
			})
			if persistErr != nil {
				log.Printf("hci-sim run persistence failed: %v", persistErr)
				if errors.Is(persistErr, database.ErrIdempotencyConflict) {
					http.Error(w, persistErr.Error(), http.StatusConflict)
				} else {
					http.Error(w, "hci_sim persistence unavailable", http.StatusServiceUnavailable)
				}
				return
			}
			runID = record.ExternalID
			runVersion = record.Version
		}
		expires := now.Add(15 * time.Minute)
		claims := lease.Claims{JTI: runID + "-1", LeaseID: "lease-" + runID, TestRunID: runID, ScenarioID: "kbd-" + request.KBDID + "-" + fixtureVariant, SupportID: request.KBDID, KBDRevision: router.KBD().Revision, BundleDigest: router.BundleDigest(), FixtureVariant: fixtureVariant, ToolContractRevision: router.Contracts().ToolRevision, PolicyRevision: router.Contracts().PolicyRevision, VirtualNodeID: "SIM-HCI-NODE-01", Container: "host", ExecutionMode: "sim-ssh", Issuer: env("HCI_SIM_LEASE_ISSUER", "hci-platform"), Audience: env("HCI_SIM_LEASE_AUDIENCE", "hci-sim"), IssuedAt: now.Unix(), NotBefore: now.Unix(), ExpiresAt: expires.Unix(), RunDeadline: expires.Unix(), MaxSessions: 4, MaxCommands: 200, MaxOutputBytes: int64(router.OutputLimit())}
		token, err := lease.Sign(secret, claims)
		if err != nil {
			http.Error(w, "lease signing failed", http.StatusInternalServerError)
			return
		}
		if runRepository != nil {
			if _, persistErr := runRepository.RecordLease(r.Context(), runID, runVersion, 1, env("HCI_SIM_RUNTIME_ID", "hci-sim"), digestValue(runID+"-1")); persistErr != nil {
				log.Printf("hci-sim lease persistence failed: %v", persistErr)
				if errors.Is(persistErr, database.ErrRunVersionConflict) {
					http.Error(w, persistErr.Error(), http.StatusConflict)
				} else {
					http.Error(w, "hci_sim lease persistence unavailable", http.StatusServiceUnavailable)
				}
				return
			}
			if _, persistErr := runRepository.AppendEvent(r.Context(), runID, 1, "lease.created", digestValue(map[string]any{"test_run_id": runID, "bundle_digest": router.BundleDigest()}), ""); persistErr != nil {
				log.Printf("hci-sim event persistence failed: %v", persistErr)
				http.Error(w, "hci_sim event persistence unavailable", http.StatusServiceUnavailable)
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		environmentContext = simulationEnvironmentContext(router, runID, strings.TrimSpace(request.CaseID), fixtureVariant)
		_ = json.NewEncoder(w).Encode(map[string]any{"test_run_id": runID, "case_id": strings.TrimSpace(request.CaseID), "support_id": request.KBDID, "bundle_digest": router.BundleDigest(), "synthetic": router.IsSynthetic(), "environment_context": environmentContext, "connection": map[string]any{"host": env("HCI_SIM_SSH_HOST", "hci-sim.hci-sim-dev.svc"), "port": strings.TrimPrefix(env("HCI_SIM_SSH_LISTEN", ":2222"), ":"), "username": "sim", "auth_type": "lease", "password": token, "execution_mode": "sim-ssh", "test_run_id": runID}})
	})
	mux.HandleFunc("/v1/simulations/test-runs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var request struct {
			KBDID              string         `json:"kbd_id"`
			CaseID             string         `json:"case_id"`
			Title              string         `json:"title"`
			Description        string         `json:"description"`
			Connection         map[string]any `json:"connection"`
			EnvironmentContext map[string]any `json:"environment_context"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32768)).Decode(&request); err != nil || strings.TrimSpace(request.KBDID) == "" || strings.TrimSpace(request.CaseID) == "" || strings.TrimSpace(request.Title) == "" || strings.TrimSpace(request.Description) == "" {
			http.Error(w, "kbd_id, case_id, title and description are required", http.StatusBadRequest)
			return
		}
		connectionRunID, connectionRunOK := request.Connection["test_run_id"].(string)
		connectionCaseID, connectionCaseOK := request.Connection["case_id"].(string)
		contextRunID, contextRunOK := request.EnvironmentContext["test_run_id"].(string)
		contextSupportID, supportOK := request.EnvironmentContext["support_id"].(string)
		contextCaseID, caseOK := request.EnvironmentContext["case_id"].(string)
		contextRevision, revisionOK := jsonInt(request.EnvironmentContext["kbd_revision"])
		if request.Connection["execution_mode"] != "sim-ssh" || request.EnvironmentContext["execution_mode"] != "sim-ssh" || !connectionRunOK || !connectionCaseOK || !contextRunOK || !supportOK || !caseOK || !revisionOK || connectionRunID == "" || connectionRunID != contextRunID || connectionCaseID != strings.TrimSpace(request.CaseID) || contextSupportID != request.KBDID || contextCaseID != strings.TrimSpace(request.CaseID) {
			http.Error(w, "sim TestRun must be explicitly bound to sim-ssh context", http.StatusConflict)
			return
		}
		status := "created"
		version := 0
		if runRepository != nil {
			record, getErr := runRepository.Get(r.Context(), connectionRunID)
			if getErr != nil {
				http.Error(w, "test_run_not_found", http.StatusConflict)
				return
			}
			if record.SupportID != request.KBDID || record.KBDRevision != contextRevision || record.ExecutionMode != "sim-ssh" || record.BundleDigest != fmt.Sprint(request.EnvironmentContext["bundle_digest"]) {
				http.Error(w, "test_run_context_mismatch", http.StatusConflict)
				return
			}
			if bindErr := runRepository.BindCase(r.Context(), connectionRunID, request.CaseID); bindErr != nil {
				http.Error(w, "test_run_case_binding_conflict", http.StatusConflict)
				return
			}
			if refreshed, refreshErr := runRepository.Get(r.Context(), connectionRunID); refreshErr == nil {
				record = refreshed
			}
			if record.Status == "requested" {
				if updated, updateErr := runRepository.UpdateStatusCAS(r.Context(), connectionRunID, record.Version, "preparing"); updateErr != nil {
					http.Error(w, "test_run_state_conflict", http.StatusConflict)
					return
				} else {
					record = updated
				}
			}
			status, version = record.Status, record.Version
			if _, eventErr := runRepository.AppendEvent(r.Context(), connectionRunID, 1, "test_run.created", digestValue(map[string]any{"title": request.Title, "description": request.Description}), ""); eventErr != nil {
				http.Error(w, "hci_sim event persistence unavailable", http.StatusServiceUnavailable)
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"case_id": strings.TrimSpace(request.CaseID), "test_run_id": connectionRunID, "status": status, "version": version, "execution_mode": "sim-ssh"})
	})
	mux.HandleFunc("/v1/simulations/test-runs/", func(w http.ResponseWriter, r *http.Request) {
		if !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		path := strings.TrimPrefix(r.URL.Path, "/v1/simulations/test-runs/")
		const contextSuffix = "/context"
		if r.Method == http.MethodGet && strings.HasSuffix(path, contextSuffix) {
			externalID := strings.TrimSuffix(path, contextSuffix)
			caseID := strings.TrimSpace(r.URL.Query().Get("case_id"))
			if externalID == "" || caseID == "" || runRepository == nil {
				http.Error(w, "test_run_id, case_id and hci_sim persistence are required", http.StatusBadRequest)
				return
			}
			record, getErr := runRepository.Get(r.Context(), externalID)
			if getErr != nil {
				http.Error(w, "test_run_not_found", http.StatusNotFound)
				return
			}
			var contextValue map[string]any
			if json.Unmarshal(record.EnvironmentContext, &contextValue) != nil || contextValue == nil {
				http.Error(w, "test_run_context_invalid", http.StatusConflict)
				return
			}
			if record.ExecutionMode != "sim-ssh" || contextValue["execution_mode"] != "sim-ssh" || strings.TrimSpace(fmt.Sprint(contextValue["case_id"])) != caseID {
				http.Error(w, "test_run_context_binding_mismatch", http.StatusConflict)
				return
			}
			if time.Now().UTC().After(record.Deadline) || terminalRunStatus(record.Status) {
				http.Error(w, "test_run_context_not_active", http.StatusConflict)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"test_run_id": record.ExternalID,
				"case_id":     caseID,
				"status":      record.Status,
				"version":     record.Version,
				"context":     contextValue,
			})
			return
		}
		if r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		const suffix = "/result"
		if !strings.HasSuffix(path, suffix) || strings.TrimSuffix(path, suffix) == "" {
			http.NotFound(w, r)
			return
		}
		externalID := strings.TrimSuffix(path, suffix)
		var request struct {
			AttemptNo     int    `json:"attempt_no"`
			OracleVersion string `json:"oracle_version"`
			Outcome       string `json:"outcome"`
			ReportURI     string `json:"report_uri"`
			ReportDigest  string `json:"report_digest"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384)).Decode(&request); err != nil {
			http.Error(w, "invalid result payload", http.StatusBadRequest)
			return
		}
		if runRepository == nil {
			http.Error(w, "hci_sim persistence is required", http.StatusServiceUnavailable)
			return
		}
		record, resultErr := runRepository.RecordResult(r.Context(), externalID, request.AttemptNo, request.OracleVersion, request.Outcome, request.ReportURI, request.ReportDigest)
		if resultErr != nil {
			status := http.StatusConflict
			if errors.Is(resultErr, database.ErrResultConflict) {
				status = http.StatusConflict
			} else if strings.Contains(resultErr.Error(), "invalid") {
				status = http.StatusBadRequest
			} else if strings.Contains(resultErr.Error(), "not_found") {
				status = http.StatusConflict
			} else {
				status = http.StatusServiceUnavailable
			}
			http.Error(w, resultErr.Error(), status)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"test_run_id": record.ExternalID, "status": record.Status, "version": record.Version, "execution_mode": record.ExecutionMode})
	})
	if runRepository != nil {
		go reconciler.Run(ctx, runRepository, reconciler.Config{
			WebhookURL:  env("HCI_SIM_OUTBOX_WEBHOOK_URL", ""),
			Interval:    envDuration("HCI_SIM_OUTBOX_INTERVAL", 2*time.Second),
			MaxAttempts: envInt("HCI_SIM_OUTBOX_MAX_ATTEMPTS", 8),
		})
	}
	httpServer.Handler = mux
	serverErrors := make(chan error, 2)
	go func() {
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErrors <- err
		}
	}()
	go func() { serverErrors <- sshServer.Serve(ctx) }()
	select {
	case <-ctx.Done():
	case err := <-serverErrors:
		if err != nil {
			return err
		}
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = sshServer.Close()
	return httpServer.Shutdown(shutdownCtx)
}

func simulationEnvironmentContext(router *fixture.Router, runID, caseID, variant string) map[string]any {
	components := make([]string, 0)
	for _, component := range strings.Split(env("HCI_SIM_COMPONENTS", "虚拟机"), ",") {
		if value := strings.TrimSpace(component); value != "" {
			components = append(components, value)
		}
	}
	return map[string]any{
		"simulation":      true,
		"execution_mode":  "sim-ssh",
		"test_run_id":     runID,
		"case_id":         caseID,
		"scenario_id":     "kbd-" + router.KBD().SupportID + "-" + variant,
		"support_id":      router.KBD().SupportID,
		"kbd_revision":    router.KBD().Revision,
		"bundle_digest":   router.BundleDigest(),
		"product":         env("HCI_SIM_PRODUCT", "HCI"),
		"version":         env("HCI_SIM_PRODUCT_VERSION", "6.11.1_R1"),
		"components":      components,
		"topology":        []string{},
		"virtual_node_id": "SIM-HCI-NODE-01",
		"node_ip":         env("HCI_SIM_SSH_HOST", "hci-sim.hci-sim-dev.svc"),
		"container":       "host",
		"authority_scope": env("HCI_SIM_AUTHORITY_SCOPE", "runtime_fixture"),
		"active_revision": envInt("HCI_SIM_ACTIVE_REVISION", router.KBD().Revision),
	}
}

func simulationBuildable(requestedID string, runtimeKBD fixture.KBDRef, activeRevision int, bundleDigest, authorityScope string, synthetic bool) bool {
	return requestedID == runtimeKBD.SupportID &&
		activeRevision == runtimeKBD.Revision &&
		bundleDigest != "" &&
		authorityScope != "runtime_fixture" &&
		!synthetic
}

func terminalRunStatus(status string) bool {
	switch status {
	case "passed", "failed", "inconclusive", "cancelled", "expired":
		return true
	default:
		return false
	}
}

func runLease(args []string) error {
	flags := flag.NewFlagSet("lease", flag.ContinueOnError)
	scenarioID := flags.String("scenario", "", "场景 ID（默认由 Bundle 的 KBD support_id 生成）")
	testRunID := flags.String("test-run", "", "测试运行 ID")
	variant := flags.String("variant", "positive-realistic", "fixture variant")
	virtualNode := flags.String("virtual-node", "SIM-HCI-NODE-01", "虚拟节点")
	ttl := flags.Duration("ttl", 2*time.Hour, "租约有效期")
	maxSessions := flags.Int("max-sessions", 64, "最大 SSH 会话数")
	maxCommands := flags.Int("max-commands", 200, "最大命令数")
	maxOutputBytes := flags.Int64("max-output-bytes", 0, "最大输出字节数（默认使用 Bundle 限制）")
	container := flags.String("container", "host", "目标容器")
	if err := flags.Parse(args); err != nil {
		return err
	}
	router, err := fixture.Load(env("HCI_SIM_FIXTURE_MANIFEST", "/etc/hci-sim/fixture-manifest.json"))
	if err != nil {
		return err
	}
	secret := []byte(strings.TrimSpace(os.Getenv("HCI_SIM_LEASE_HMAC_KEY")))
	if len(secret) < 32 {
		return errors.New("HCI_SIM_LEASE_HMAC_KEY 至少需要 32 字节")
	}
	if *maxOutputBytes == 0 {
		*maxOutputBytes = int64(router.OutputLimit())
	}
	now := time.Now().UTC()
	if strings.TrimSpace(*testRunID) == "" {
		*testRunID = "run-" + now.Format("20060102T150405Z")
	}
	if strings.TrimSpace(*scenarioID) == "" {
		*scenarioID = "kbd-" + router.KBD().SupportID
	}
	leaseID := "lease-" + randomID()
	token, err := lease.Sign(secret, lease.Claims{
		JTI: leaseID, LeaseID: leaseID, TestRunID: *testRunID, ScenarioID: *scenarioID,
		SupportID: router.KBD().SupportID, KBDRevision: router.KBD().Revision, BundleDigest: router.BundleDigest(), FixtureVariant: *variant,
		ToolContractRevision: router.Contracts().ToolRevision, PolicyRevision: router.Contracts().PolicyRevision,
		VirtualNodeID: *virtualNode, Container: *container, ExecutionMode: "sim-ssh", Issuer: env("HCI_SIM_LEASE_ISSUER", "hci-platform"), Audience: env("HCI_SIM_LEASE_AUDIENCE", "hci-sim"),
		IssuedAt: now.Unix(), NotBefore: now.Unix(), ExpiresAt: now.Add(*ttl).Unix(), RunDeadline: now.Add(*ttl).Unix(),
		MaxSessions: *maxSessions, MaxCommands: *maxCommands, MaxOutputBytes: *maxOutputBytes,
	})
	if err != nil {
		return err
	}
	// 该子命令只把 token 输出给调用方，服务端日志永不记录 token。
	fmt.Println(token)
	return nil
}

func runManifestDigest(args []string) error {
	flags := flag.NewFlagSet("manifest-digest", flag.ContinueOnError)
	path := flags.String("manifest", env("HCI_SIM_FIXTURE_MANIFEST", "/etc/hci-sim/fixture-manifest.json"), "Manifest 文件路径")
	if err := flags.Parse(args); err != nil {
		return err
	}
	raw, err := os.ReadFile(*path)
	if err != nil {
		return err
	}
	digest, err := fixture.DigestFromJSON(raw)
	if err != nil {
		return err
	}
	fmt.Println(digest)
	return nil
}

func loadHostSigner(path string) (ssh.Signer, error) {
	if raw, err := os.ReadFile(path); err == nil {
		return ssh.ParsePrivateKey(raw)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("读取 SSH host key 失败: %w", err)
	}
	// 仅本地单元/Spike 允许临时 key；K3s 必须挂载 Secret 文件。
	if os.Getenv("HCI_SIM_ALLOW_EPHEMERAL_HOST_KEY") != "true" {
		return nil, fmt.Errorf("SSH host key 不存在: %s", path)
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, err
	}
	return ssh.NewSignerFromKey(key)
}

func env(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

// simulationFixtureVariant keeps the local synthetic bootstrap default while
// allowing a published Runtime Bundle to select its declared route variant.
// Helm injects positive-realistic for the built-in 27123 Bundle.
func simulationFixtureVariant() string {
	if value := strings.TrimSpace(os.Getenv("HCI_SIM_FIXTURE_VARIANT")); value != "" {
		return value
	}
	return "positive-minimal"
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value, err := time.ParseDuration(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func randomID() string {
	buffer := make([]byte, 12)
	if _, err := rand.Read(buffer); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return fmt.Sprintf("%x", buffer)
}

// digestValue 为控制面幂等和 Scenario 指纹提供稳定摘要；摘要中不包含
// Lease、密码、私钥或原始 Artifact。
func digestValue(value any) string {
	payload, err := json.Marshal(value)
	if err != nil {
		return "sha256:invalid"
	}
	sum := sha256.Sum256(payload)
	return fmt.Sprintf("sha256:%x", sum[:])
}

func jsonInt(value any) (int, bool) {
	floatValue, ok := value.(float64)
	if !ok || floatValue != float64(int(floatValue)) {
		return 0, false
	}
	return int(floatValue), true
}

type repositoryEventRecorder struct {
	repository *database.RunRepository
}

func (r repositoryEventRecorder) RecordEvent(ctx context.Context, externalID string, attemptNo int, eventType, payloadDigest, traceID string) error {
	_, err := r.repository.AppendEvent(ctx, externalID, attemptNo, eventType, payloadDigest, traceID)
	return err
}
