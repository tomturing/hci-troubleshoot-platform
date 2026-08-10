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
	if err := runServer(); err != nil {
		log.Fatal(err)
	}
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
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ready\n")) })
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
	mux.HandleFunc("/v1/simulations/build", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var request struct {
			KBDID string `json:"kbd_id"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&request); err != nil || strings.TrimSpace(request.KBDID) == "" {
			http.Error(w, "kbd_id is required", http.StatusBadRequest)
			return
		}
		if router.KBD().SupportID != strings.TrimSpace(request.KBDID) {
			http.Error(w, "capability_gap: requested KBD is not the loaded immutable fixture", http.StatusConflict)
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
		runVersion := 1
		if runRepository != nil {
			record, persistErr := runRepository.Create(r.Context(), database.RunInput{
				ExternalID: runID, SupportID: request.KBDID, KBDRevision: router.KBD().Revision,
				Variant: fixtureVariant, BundleDigest: router.BundleDigest(), ExecutionMode: "sim-ssh",
				IdempotencyKey: idempotencyKey, RequestDigest: requestDigest, Deadline: now.Add(15 * time.Minute),
				InputFingerprint:   inputFingerprint,
				EnvironmentContext: map[string]any{"test_run_id": runID, "support_id": request.KBDID, "kbd_revision": router.KBD().Revision, "bundle_digest": router.BundleDigest(), "execution_mode": "sim-ssh", "virtual_node_id": "SIM-HCI-NODE-01", "container": "host"},
			})
			if persistErr != nil {
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
				if errors.Is(persistErr, database.ErrRunVersionConflict) {
					http.Error(w, persistErr.Error(), http.StatusConflict)
				} else {
					http.Error(w, "hci_sim lease persistence unavailable", http.StatusServiceUnavailable)
				}
				return
			}
			if _, persistErr := runRepository.AppendEvent(r.Context(), runID, 1, "lease.created", digestValue(map[string]any{"test_run_id": runID, "bundle_digest": router.BundleDigest()}), ""); persistErr != nil {
				http.Error(w, "hci_sim event persistence unavailable", http.StatusServiceUnavailable)
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"test_run_id": runID, "support_id": request.KBDID, "bundle_digest": router.BundleDigest(), "synthetic": router.IsSynthetic(), "environment_context": map[string]any{"test_run_id": runID, "support_id": request.KBDID, "kbd_revision": router.KBD().Revision, "bundle_digest": router.BundleDigest(), "execution_mode": "sim-ssh", "virtual_node_id": "SIM-HCI-NODE-01", "container": "host"}, "connection": map[string]any{"host": env("HCI_SIM_SSH_HOST", "hci-sim.hci-sim-dev.svc"), "port": strings.TrimPrefix(env("HCI_SIM_SSH_LISTEN", ":2222"), ":"), "username": "sim", "auth_type": "lease", "password": token, "execution_mode": "sim-ssh", "test_run_id": runID}})
	})
	mux.HandleFunc("/v1/simulations/test-runs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var request struct {
			KBDID              string         `json:"kbd_id"`
			Title              string         `json:"title"`
			Description        string         `json:"description"`
			Connection         map[string]any `json:"connection"`
			EnvironmentContext map[string]any `json:"environment_context"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32768)).Decode(&request); err != nil || strings.TrimSpace(request.KBDID) == "" || strings.TrimSpace(request.Title) == "" || strings.TrimSpace(request.Description) == "" {
			http.Error(w, "kbd_id, title and description are required", http.StatusBadRequest)
			return
		}
		connectionRunID, connectionRunOK := request.Connection["test_run_id"].(string)
		contextRunID, contextRunOK := request.EnvironmentContext["test_run_id"].(string)
		contextSupportID, supportOK := request.EnvironmentContext["support_id"].(string)
		contextRevision, revisionOK := jsonInt(request.EnvironmentContext["kbd_revision"])
		if request.Connection["execution_mode"] != "sim-ssh" || request.EnvironmentContext["execution_mode"] != "sim-ssh" || !connectionRunOK || !contextRunOK || !supportOK || !revisionOK || connectionRunID == "" || connectionRunID != contextRunID || contextSupportID != request.KBDID {
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
		_ = json.NewEncoder(w).Encode(map[string]any{"case_id": "sim-case-" + randomID(), "test_run_id": connectionRunID, "status": status, "version": version, "execution_mode": "sim-ssh"})
	})
	mux.HandleFunc("/v1/simulations/test-runs/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !controlAuthorized(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		const suffix = "/result"
		path := strings.TrimPrefix(r.URL.Path, "/v1/simulations/test-runs/")
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

func runLease(args []string) error {
	flags := flag.NewFlagSet("lease", flag.ContinueOnError)
	scenarioID := flags.String("scenario", "kbd-27123", "场景 ID")
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
