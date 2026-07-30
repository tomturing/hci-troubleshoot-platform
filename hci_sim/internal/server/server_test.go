package server

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"strings"
	"testing"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
	"hci_sim/internal/metrics"

	"golang.org/x/crypto/ssh"
)

const testManifest = `{
  "schema_version":"1.0",
  "kbd":{"support_id":"27123","revision":1,"checksum":"abc"},
  "variables":{"VM":"271230001"},
  "routes":[
    {"id":"sig1","signal_id":"sig_001","variant":"positive-realistic","command_pattern":"acli --formatter json task get -k '启动虚拟机' -s failed -l 1","exit_code":0,"stdout":"{\"data\":[{\"vm\":\"{{VM}}\"}]}\n","chunk_bytes":8}
  ]
}`

func TestSSHExecUsesLeaseFixtureAndFailsClosed(t *testing.T) {
	router, err := fixture.Parse([]byte(testManifest))
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
	srv, err := New(Config{
		ListenAddress: "127.0.0.1:0", HostSigner: signer, LeaseSecret: secret,
		Router: router, Workers: 2, QueueSize: 4, Metrics: &metrics.Metrics{},
	})
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
	token, err := lease.Sign(secret, lease.Claims{
		LeaseID: "lease-test", TestRunID: "run-test", ScenarioID: "scenario-27123",
		FixtureManifestHash: router.ManifestHash(), FixtureVariant: "positive-realistic",
		ExecutionMode: "sim-ssh", IssuedAt: now.Unix(), ExpiresAt: now.Add(time.Hour).Unix(),
		MaxSessions: 8, MaxCommands: 8,
	})
	if err != nil {
		t.Fatal(err)
	}
	client, err := ssh.Dial("tcp", srv.Addr(), &ssh.ClientConfig{
		User: "sim", Auth: []ssh.AuthMethod{ssh.Password(token)},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), Timeout: 2 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	session, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Setenv("HTP_EXEC_ID", "exec-test"); err != nil {
		t.Fatal(err)
	}
	output, err := session.CombinedOutput("acli --formatter json task get -k '启动虚拟机' -s failed -l 1")
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
}
