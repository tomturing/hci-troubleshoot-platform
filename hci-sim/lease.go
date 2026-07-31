package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strconv"
	"time"
)

// LeaseVerifier 校验 scenario lease（方案 5.3）。
// P0：leaseSecret 为空时关闭校验（仅占位，便于后续接入后端签发的租约）。
type LeaseVerifier struct {
	secret string
}

func NewLeaseVerifier(secret string) *LeaseVerifier {
	return &LeaseVerifier{secret: secret}
}

// Encode 由后端签发一个租约 token（HMAC(secret, scenario_id|expires_at)）。
func (v *LeaseVerifier) Encode(scenarioID string, expiresAt int64) string {
	if v.secret == "" {
		return ""
	}
	msg := scenarioID + "|" + strconv.FormatInt(expiresAt, 10)
	mac := hmac.New(sha256.New, []byte(v.secret))
	mac.Write([]byte(msg))
	return hex.EncodeToString(mac.Sum(nil)) + "." + strconv.FormatInt(expiresAt, 10)
}

// Verify 校验租约 token（secret 为空时直接通过）。
func (v *LeaseVerifier) Verify(token, scenarioID string) bool {
	if v.secret == "" {
		return true
	}
	if token == "" {
		return false
	}
	parts := split2(token, '.')
	if len(parts) != 2 {
		return false
	}
	expiresAt, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil {
		return false
	}
	if expiresAt != 0 && expiresAt < time.Now().UnixMilli() {
		return false // 租约过期
	}
	expected := v.Encode(scenarioID, expiresAt)
	if len(expected) < 64 || len(token) < 64 {
		return false
	}
	return hmac.Equal([]byte(expected[:64]), []byte(token[:64]))
}

func split2(s string, sep byte) []string {
	for i := 0; i < len(s); i++ {
		if s[i] == sep {
			return []string{s[:i], s[i+1:]}
		}
	}
	return []string{s}
}

// verifyLease 是 handler 使用的便捷函数（secret 为空即不校验）。
func verifyLease(secret, token, scenarioID string) bool {
	return NewLeaseVerifier(secret).Verify(token, scenarioID)
}

var errBadInt = errors.New("bad int")
