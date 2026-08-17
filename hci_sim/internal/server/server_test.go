package server

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
	"hci_sim/internal/metrics"

	"golang.org/x/crypto/ssh"
)

type recordedEvent struct {
	externalID string
	eventType  string
	digest     string
}

type memoryEventRecorder struct {
	mu     sync.Mutex
	events []recordedEvent
}

func (r *memoryEventRecorder) RecordEvent(_ context.Context, externalID string, _ int, eventType, payloadDigest, _ string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, recordedEvent{externalID: externalID, eventType: eventType, digest: payloadDigest})
	return nil
}

func (r *memoryEventRecorder) snapshot() []recordedEvent {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]recordedEvent(nil), r.events...)
}

func serverManifest(t *testing.T) []byte {
	t.Helper()
	manifest := fixture.Manifest{
		SchemaVersion: fixture.SchemaVersion,
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: "27123", Revision: 24, Checksum: "sha256:kbd"},
		Contracts:     fixture.Contracts{ToolRevision: "tool-r24", PolicyRevision: "policy-r2"},
		Limits:        fixture.Limits{MaxRoutes: 10, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes: []fixture.Route{{
			ID: "sig1", SignalID: "sig_001", Variant: "positive-realistic",
			RouteKey: fixture.RouteKey{Tool: "acli", AcquisitionKey: "acli:--formatter", Argv: []string{"acli", "--formatter", "json", "task", "get", "-k", "启动虚拟机", "-s", "failed", "-l", "1"}, Node: "SIM-NODE", Container: "host"},
			Result:   fixture.ResultDef{ExitCode: 0, Stdout: "{\"data\":[{\"vm\":\"271230001\"}]}\n"}, Fault: fixture.FaultDef{Type: fixture.FaultNone},
		}},
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func testClaims(router *fixture.Router, now time.Time) lease.Claims {
	return lease.Claims{
		JTI: "jti-test", LeaseID: "lease-test", TestRunID: "run-test", ScenarioID: "scenario-27123",
		SupportID: router.KBD().SupportID, KBDRevision: router.KBD().Revision, BundleDigest: router.BundleDigest(), FixtureVariant: "positive-realistic",
		ToolContractRevision: router.Contracts().ToolRevision, PolicyRevision: router.Contracts().PolicyRevision, VirtualNodeID: "SIM-NODE", Container: "host",
		ExecutionMode: "sim-ssh", Issuer: "hci-platform", Audience: "hci-sim", IssuedAt: now.Unix(), NotBefore: now.Unix(),
		ExpiresAt: now.Add(time.Hour).Unix(), RunDeadline: now.Add(time.Hour).Unix(), MaxSessions: 8, MaxCommands: 8, MaxOutputBytes: 4096,
	}
}

func serverRouterFor(t *testing.T, supportID, stdout string) *fixture.Router {
	t.Helper()
	var manifest fixture.Manifest
	if err := json.Unmarshal(serverManifest(t), &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.KBD.SupportID = supportID
	manifest.Routes[0].Result.Stdout = stdout
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	router, err := fixture.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return router
}

func TestSSHExecUsesLeaseFixtureAndFailsClosed(t *testing.T) {
	router, err := fixture.Parse(serverManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(privateKey)
	if err != nil {
		t.Fatal(err)
	}
	secret := []byte("0123456789abcdef0123456789abcdef")
	recorder := &memoryEventRecorder{}
	srv, err := New(Config{ListenAddress: "127.0.0.1:0", HostSigner: signer, LeaseSecret: secret, Router: router, Workers: 2, QueueSize: 4, MaxOutputBytes: 4096, LeaseIssuer: "hci-platform", LeaseAudience: "hci-sim", Metrics: &metrics.Metrics{}, Recorder: recorder})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = srv.Serve(ctx) }()
	deadline := time.Now().Add(2 * time.Second)
	for srv.Addr() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if srv.Addr() == "" {
		t.Fatal("SSH server 未开始监听")
	}
	now := time.Now()
	token, err := lease.Sign(secret, testClaims(router, now))
	if err != nil {
		t.Fatal(err)
	}
	client, err := ssh.Dial("tcp", srv.Addr(), &ssh.ClientConfig{User: "sim", Auth: []ssh.AuthMethod{ssh.Password(token)}, HostKeyCallback: ssh.InsecureIgnoreHostKey(), Timeout: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	session, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	output, err := session.CombinedOutput("acli --formatter=json task get -k '启动虚拟机' -s failed -l 1")
	if err != nil || !strings.Contains(string(output), "271230001") {
		t.Fatalf("fixture 执行失败: output=%q err=%v", output, err)
	}
	unknown, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	output, err = unknown.CombinedOutput("uname -a")
	exitErr, ok := err.(*ssh.ExitError)
	if !ok || exitErr.ExitStatus() != exitFixtureNotFound || !strings.Contains(string(output), "fixture_not_found") {
		t.Fatalf("未知命令未 fail closed: output=%q err=%v", output, err)
	}
	events := recorder.snapshot()
	if len(events) != 2 || events[0].externalID != "run-test" || events[0].eventType != "exec.done" || events[1].eventType != "fixture_not_found" {
		t.Fatalf("unexpected persisted event metadata: %+v", events)
	}
	for _, event := range events {
		if !strings.HasPrefix(event.digest, "sha256:") || len(event.digest) != 71 {
			t.Fatalf("event payload is not a redacted digest: %+v", event)
		}
	}
}

func TestServerRejectsLeaseAfterExpiryOrRevocation(t *testing.T) {
	router, err := fixture.Parse(serverManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	_, privateKey, _ := ed25519.GenerateKey(rand.Reader)
	signer, _ := ssh.NewSignerFromKey(privateKey)
	secret := []byte("0123456789abcdef0123456789abcdef")
	srv, err := New(Config{ListenAddress: "127.0.0.1:0", HostSigner: signer, LeaseSecret: secret, Router: router, Workers: 1, QueueSize: 1, MaxOutputBytes: 4096, LeaseIssuer: "hci-platform", LeaseAudience: "hci-sim", Metrics: &metrics.Metrics{}})
	if err != nil {
		t.Fatal(err)
	}
	claims := testClaims(router, time.Now())
	claims.ExpiresAt = time.Now().Add(-time.Second).Unix()
	if _, err := lease.Sign(secret, claims); err == nil {
		t.Fatal("过期 claims 不应被签发")
	}
	claims = testClaims(router, time.Now())
	srv.tracker.Revoke(claims.JTI, time.Now().Add(time.Hour))
	if err := srv.tracker.AuthorizeCommand(claims, time.Now()); err == nil {
		t.Fatal("revoke 后每命令复验必须拒绝")
	}
}

func TestBundlePoolBindsLeaseToExactKBDContracts(t *testing.T) {
	router27123 := serverRouterFor(t, "27123", "bundle=27123\n")
	router23821 := serverRouterFor(t, "23821", "bundle=23821\n")
	pool, err := fixture.NewBundlePool(router27123, router23821)
	if err != nil {
		t.Fatal(err)
	}
	_, privateKey, _ := ed25519.GenerateKey(rand.Reader)
	signer, _ := ssh.NewSignerFromKey(privateKey)
	secret := []byte("0123456789abcdef0123456789abcdef")
	srv, err := New(Config{ListenAddress: "127.0.0.1:0", HostSigner: signer, LeaseSecret: secret, Pool: pool, Workers: 1, QueueSize: 2, MaxOutputBytes: 4096, LeaseIssuer: "hci-platform", LeaseAudience: "hci-sim", Metrics: &metrics.Metrics{}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = srv.Serve(ctx) }()
	deadline := time.Now().Add(2 * time.Second)
	for srv.Addr() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}

	claims := testClaims(router23821, time.Now())
	token, err := lease.Sign(secret, claims)
	if err != nil {
		t.Fatal(err)
	}
	client, err := ssh.Dial("tcp", srv.Addr(), &ssh.ClientConfig{User: "sim", Auth: []ssh.AuthMethod{ssh.Password(token)}, HostKeyCallback: ssh.InsecureIgnoreHostKey(), Timeout: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	session, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	output, err := session.CombinedOutput("acli --formatter=json task get -k '启动虚拟机' -s failed -l 1")
	_ = client.Close()
	if err != nil || string(output) != "bundle=23821\n" {
		t.Fatalf("23821 租约未路由到对应 Bundle: output=%q err=%v", output, err)
	}

	claims.BundleDigest = router27123.BundleDigest()
	attackToken, err := lease.Sign(secret, claims)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ssh.Dial("tcp", srv.Addr(), &ssh.ClientConfig{User: "sim", Auth: []ssh.AuthMethod{ssh.Password(attackToken)}, HostKeyCallback: ssh.InsecureIgnoreHostKey(), Timeout: 2 * time.Second}); err == nil {
		t.Fatal("support_id 与 digest 交叉绑定的合法签名租约未被拒绝")
	}
}
