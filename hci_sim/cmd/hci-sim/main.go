package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
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
		LeaseIssuer: env("HCI_SIM_LEASE_ISSUER", "hci-platform"), LeaseAudience: env("HCI_SIM_LEASE_AUDIENCE", "hci-sim"), Metrics: prom,
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
		})
	})
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

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
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
