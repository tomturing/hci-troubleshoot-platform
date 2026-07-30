package lease

import (
	"testing"
	"time"
)

func validClaims(now time.Time) Claims {
	return Claims{
		LeaseID: "lease-1", TestRunID: "run-1", ScenarioID: "scenario-27123",
		FixtureManifestHash: "sha256:fixture", FixtureVariant: "positive-realistic",
		ExecutionMode: "sim-ssh", IssuedAt: now.Unix(), ExpiresAt: now.Add(time.Hour).Unix(),
		MaxSessions: 2, MaxCommands: 4,
	}
}

func TestSignValidateAndManifestBinding(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	token, err := Sign([]byte("0123456789abcdef0123456789abcdef"), validClaims(now))
	if err != nil {
		t.Fatal(err)
	}
	if !IsToken(token) {
		t.Fatal("签发结果没有 htp1 前缀")
	}
	claims, err := Validate([]byte("0123456789abcdef0123456789abcdef"), token, "sha256:fixture", now)
	if err != nil || claims.ScenarioID != "scenario-27123" {
		t.Fatalf("校验失败: claims=%+v err=%v", claims, err)
	}
	if _, err := Validate([]byte("0123456789abcdef0123456789abcdef"), token, "sha256:stale", now); err == nil {
		t.Fatal("manifest 漂移必须 fail closed")
	}
	if _, err := Validate([]byte("0123456789abcdef0123456789abcdef"), token, "sha256:fixture", now.Add(2*time.Hour)); err == nil {
		t.Fatal("过期 Lease 必须 fail closed")
	}
	tampered := token[:len(token)-1] + "A"
	if _, err := Validate([]byte("0123456789abcdef0123456789abcdef"), tampered, "sha256:fixture", now); err == nil {
		t.Fatal("签名被篡改的 Lease 必须 fail closed")
	}
}

func TestTrackerEnforcesSessionAndCommandQuota(t *testing.T) {
	now := time.Now().Truncate(time.Second)
	claims := validClaims(now)
	tracker := NewTracker()
	release1, err := tracker.AcquireSession(claims)
	if err != nil {
		t.Fatal(err)
	}
	release2, err := tracker.AcquireSession(claims)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tracker.AcquireSession(claims); err == nil {
		t.Fatal("超过会话配额必须失败")
	}
	release1()
	release2()
	for i := 0; i < claims.MaxCommands; i++ {
		if err := tracker.ConsumeCommand(claims); err != nil {
			t.Fatal(err)
		}
	}
	if err := tracker.ConsumeCommand(claims); err == nil {
		t.Fatal("超过命令配额必须失败")
	}
}
