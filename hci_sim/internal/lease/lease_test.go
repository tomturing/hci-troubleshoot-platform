package lease

import (
	"sync"
	"testing"
	"time"
)

var testSecret = []byte("0123456789abcdef0123456789abcdef")

func validClaims(now time.Time) Claims {
	return Claims{
		JTI: "jti-1", LeaseID: "lease-1", TestRunID: "run-1", ScenarioID: "scenario-27123",
		SupportID: "27123", KBDRevision: 24, BundleDigest: "sha256:bundle", FixtureVariant: "positive-realistic",
		ToolContractRevision: "tool-r24", PolicyRevision: "policy-r2", VirtualNodeID: "SIM-NODE", Container: "host",
		ExecutionMode: "sim-ssh", Issuer: "hci-platform", Audience: "hci-sim",
		IssuedAt: now.Unix(), NotBefore: now.Unix(), ExpiresAt: now.Add(time.Hour).Unix(), RunDeadline: now.Add(2 * time.Hour).Unix(),
		MaxSessions: 2, MaxCommands: 4, MaxOutputBytes: 100,
	}
}

func TestSignValidateAndBundleBinding(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	token, err := Sign(testSecret, validClaims(now))
	if err != nil {
		t.Fatal(err)
	}
	if !IsToken(token) {
		t.Fatal("签发结果没有 htp2 前缀")
	}
	claims, err := Validate(testSecret, token, "sha256:bundle", "hci-platform", "hci-sim", now)
	if err != nil || claims.ScenarioID != "scenario-27123" {
		t.Fatalf("校验失败: claims=%+v err=%v", claims, err)
	}
	for _, expected := range []string{"sha256:stale", "wrong-issuer", "wrong-audience"} {
		bundle, issuer, audience := "sha256:bundle", "hci-platform", "hci-sim"
		switch expected {
		case "sha256:stale":
			bundle = expected
		case "wrong-issuer":
			issuer = expected
		case "wrong-audience":
			audience = expected
		}
		if _, err := Validate(testSecret, token, bundle, issuer, audience, now); err == nil {
			t.Fatalf("%s mismatch must fail", expected)
		}
	}
	if _, err := Validate(testSecret, token, "sha256:bundle", "hci-platform", "hci-sim", now.Add(2*time.Hour)); err == nil {
		t.Fatal("过期 Lease 必须 fail closed")
	}
	if _, err := Validate(testSecret, token[:len(token)-1]+"A", "sha256:bundle", "hci-platform", "hci-sim", now); err == nil {
		t.Fatal("篡改 Lease 必须 fail closed")
	}
}

func TestTrackerEnforcesSessionCommandOutputAndRevocation(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	claims := validClaims(now)
	tracker := NewTracker()
	release1, err := tracker.AcquireSession(claims, now)
	if err != nil {
		t.Fatal(err)
	}
	release2, err := tracker.AcquireSession(claims, now)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tracker.AcquireSession(claims, now); err == nil {
		t.Fatal("超过会话配额必须失败")
	}
	release1()
	release2()
	for i := 0; i < claims.MaxCommands; i++ {
		if err := tracker.AuthorizeCommand(claims, now); err != nil {
			t.Fatal(err)
		}
	}
	if err := tracker.AuthorizeCommand(claims, now); err == nil {
		t.Fatal("超过命令配额必须失败")
	}
	claims.JTI, claims.LeaseID, claims.MaxCommands = "jti-output", "lease-output", 4
	if err := tracker.ReserveOutput(claims, 80, now); err != nil {
		t.Fatal(err)
	}
	if err := tracker.ReserveOutput(claims, 21, now); err == nil {
		t.Fatal("超过输出配额必须失败")
	}
	tracker.Revoke(claims.JTI, now.Add(time.Hour))
	if err := tracker.AuthorizeCommand(claims, now); err == nil {
		t.Fatal("撤销 Lease 的每命令复验必须失败")
	}
}

func TestTrackerConcurrentQuotaNeverOverdraws(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	claims := validClaims(now)
	claims.MaxCommands = 10
	tracker := NewTracker()
	var wg sync.WaitGroup
	accepted := make(chan struct{}, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if tracker.AuthorizeCommand(claims, now) == nil {
				accepted <- struct{}{}
			}
		}()
	}
	wg.Wait()
	close(accepted)
	count := 0
	for range accepted {
		count++
	}
	if count != claims.MaxCommands {
		t.Fatalf("accepted=%d want=%d", count, claims.MaxCommands)
	}
}
