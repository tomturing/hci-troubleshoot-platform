// Package lease 实现 hci-sim 场景 Capability 的签发、每命令复验和单副本配额。
package lease

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

const tokenPrefix = "htp2"

var rawURL = base64.RawURLEncoding

// Claims 是 v2 Capability：每一个字段均是不可变 Run/Bundle/Target 绑定的一部分。
type Claims struct {
	JTI                  string `json:"jti"`
	LeaseID              string `json:"lease_id"`
	TestRunID            string `json:"test_run_id"`
	ScenarioID           string `json:"scenario_id"`
	SupportID            string `json:"support_id"`
	KBDRevision          int    `json:"kbd_revision"`
	BundleDigest         string `json:"bundle_digest"`
	FixtureVariant       string `json:"variant"`
	ToolContractRevision string `json:"tool_contract_revision"`
	PolicyRevision       string `json:"policy_revision"`
	VirtualNodeID        string `json:"virtual_node_id"`
	Container            string `json:"container"`
	ExecutionMode        string `json:"execution_mode"`
	Issuer               string `json:"issuer"`
	Audience             string `json:"audience"`
	IssuedAt             int64  `json:"issued_at"`
	NotBefore            int64  `json:"not_before"`
	ExpiresAt            int64  `json:"expires_at"`
	RunDeadline          int64  `json:"run_deadline"`
	MaxSessions          int    `json:"max_sessions"`
	MaxCommands          int    `json:"max_commands"`
	MaxOutputBytes       int64  `json:"max_output_bytes"`
}

// IsToken 只识别协议前缀，不执行真实性校验。
func IsToken(value string) bool { return strings.HasPrefix(strings.TrimSpace(value), tokenPrefix+".") }

// Sign 使用 HMAC-SHA256 签发紧凑租约；token 本身永不进入日志。
func Sign(secret []byte, claims Claims) (string, error) {
	if len(secret) < 32 {
		return "", errors.New("租约 HMAC key 至少需要 32 字节")
	}
	if err := validateClaims(claims, time.Now()); err != nil {
		return "", err
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", fmt.Errorf("序列化租约失败: %w", err)
	}
	encoded := rawURL.EncodeToString(payload)
	unsigned := tokenPrefix + "." + encoded
	return unsigned + "." + rawURL.EncodeToString(sign(secret, unsigned)), nil
}

// Validate 校验签名、时效、issuer/audience、mode 和 Bundle 绑定。
func Validate(secret []byte, token, expectedBundleDigest, expectedIssuer, expectedAudience string, now time.Time) (Claims, error) {
	if len(secret) < 32 {
		return Claims{}, errors.New("租约 HMAC key 至少需要 32 字节")
	}
	parts := strings.Split(strings.TrimSpace(token), ".")
	if len(parts) != 3 || parts[0] != tokenPrefix {
		return Claims{}, errors.New("场景租约格式无效")
	}
	unsigned := parts[0] + "." + parts[1]
	provided, err := rawURL.DecodeString(parts[2])
	if err != nil {
		return Claims{}, errors.New("场景租约签名编码无效")
	}
	if !hmac.Equal(provided, sign(secret, unsigned)) {
		return Claims{}, errors.New("场景租约签名无效")
	}
	payload, err := rawURL.DecodeString(parts[1])
	if err != nil {
		return Claims{}, errors.New("场景租约载荷编码无效")
	}
	var claims Claims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return Claims{}, errors.New("场景租约载荷无效")
	}
	if err := validateClaims(claims, now); err != nil {
		return Claims{}, err
	}
	if expectedBundleDigest != "" && claims.BundleDigest != expectedBundleDigest {
		return Claims{}, errors.New("场景租约绑定的 bundle 已漂移")
	}
	if expectedIssuer != "" && claims.Issuer != expectedIssuer {
		return Claims{}, errors.New("场景租约 issuer 不匹配")
	}
	if expectedAudience != "" && claims.Audience != expectedAudience {
		return Claims{}, errors.New("场景租约 audience 不匹配")
	}
	return claims, nil
}

func validateClaims(claims Claims, now time.Time) error {
	if claims.JTI == "" || claims.LeaseID == "" || claims.TestRunID == "" || claims.ScenarioID == "" {
		return errors.New("场景租约缺少运行标识")
	}
	if claims.SupportID == "" || claims.KBDRevision < 1 || claims.BundleDigest == "" || claims.FixtureVariant == "" {
		return errors.New("场景租约缺少 KBD 或 bundle 绑定")
	}
	if claims.ToolContractRevision == "" || claims.PolicyRevision == "" || claims.VirtualNodeID == "" || claims.Container == "" {
		return errors.New("场景租约缺少工具、策略或目标绑定")
	}
	if claims.ExecutionMode != "sim-ssh" {
		return errors.New("场景租约 execution_mode 必须为 sim-ssh")
	}
	if claims.Issuer == "" || claims.Audience == "" {
		return errors.New("场景租约缺少 issuer 或 audience")
	}
	if claims.IssuedAt <= 0 || claims.NotBefore < claims.IssuedAt || claims.ExpiresAt <= claims.NotBefore || claims.RunDeadline < claims.ExpiresAt {
		return errors.New("场景租约时间范围无效")
	}
	if now.Unix() < claims.NotBefore {
		return errors.New("场景租约尚未生效")
	}
	if now.Unix() >= claims.ExpiresAt || now.Unix() >= claims.RunDeadline {
		return errors.New("场景租约已过期")
	}
	if claims.IssuedAt > now.Add(2*time.Minute).Unix() {
		return errors.New("场景租约签发时间超前")
	}
	if claims.MaxSessions < 1 || claims.MaxSessions > 256 || claims.MaxCommands < 1 || claims.MaxCommands > 10000 || claims.MaxOutputBytes < 1 || claims.MaxOutputBytes > 64*1024*1024 {
		return errors.New("场景租约配额无效")
	}
	return nil
}

func sign(secret []byte, value string) []byte {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(value))
	return mac.Sum(nil)
}

// Store 预留给 Redis Lua/CAS 多副本实现；当前 Helm 强制单副本时使用 MemoryStore。
type Store interface {
	AcquireSession(Claims, time.Time) (func(), error)
	AuthorizeCommand(Claims, time.Time) error
	ReserveOutput(Claims, int64, time.Time) error
	Revoke(string, time.Time)
	IsRevoked(string, time.Time) bool
}

type Tracker struct {
	mu      sync.Mutex
	entries map[string]*usage
	revoked map[string]int64
}

type usage struct {
	sessions, commands int
	output             int64
	expires            int64
}

func NewTracker() *Tracker {
	return &Tracker{entries: make(map[string]*usage), revoked: make(map[string]int64)}
}

func (t *Tracker) AcquireSession(claims Claims, now time.Time) (func(), error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.cleanupLocked(now.Unix())
	if err := t.authorizeLocked(claims, now); err != nil {
		return nil, err
	}
	u := t.usageLocked(claims)
	if u.sessions >= claims.MaxSessions {
		return nil, errors.New("场景租约 SSH 会话配额已耗尽")
	}
	u.sessions++
	var once sync.Once
	return func() {
		once.Do(func() {
			t.mu.Lock()
			defer t.mu.Unlock()
			if current := t.entries[claims.JTI]; current != nil && current.sessions > 0 {
				current.sessions--
			}
		})
	}, nil
}

func (t *Tracker) AuthorizeCommand(claims Claims, now time.Time) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.cleanupLocked(now.Unix())
	if err := t.authorizeLocked(claims, now); err != nil {
		return err
	}
	u := t.usageLocked(claims)
	if u.commands >= claims.MaxCommands {
		return errors.New("场景租约命令配额已耗尽")
	}
	u.commands++
	return nil
}

func (t *Tracker) ReserveOutput(claims Claims, amount int64, now time.Time) error {
	if amount < 0 {
		return errors.New("输出配额不能为负数")
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.cleanupLocked(now.Unix())
	if err := t.authorizeLocked(claims, now); err != nil {
		return err
	}
	u := t.usageLocked(claims)
	if amount > claims.MaxOutputBytes-u.output {
		return errors.New("场景租约输出配额已耗尽")
	}
	u.output += amount
	return nil
}

func (t *Tracker) Revoke(jti string, until time.Time) {
	if jti == "" {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.revoked[jti] = until.Unix()
}
func (t *Tracker) IsRevoked(jti string, now time.Time) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.cleanupLocked(now.Unix())
	until, ok := t.revoked[jti]
	return ok && until > now.Unix()
}

func (t *Tracker) authorizeLocked(claims Claims, now time.Time) error {
	if err := validateClaims(claims, now); err != nil {
		return err
	}
	if until, revoked := t.revoked[claims.JTI]; revoked && until > now.Unix() {
		return errors.New("场景租约已撤销")
	}
	return nil
}

func (t *Tracker) usageLocked(claims Claims) *usage {
	if current := t.entries[claims.JTI]; current != nil {
		return current
	}
	current := &usage{expires: claims.ExpiresAt}
	t.entries[claims.JTI] = current
	return current
}
func (t *Tracker) cleanupLocked(now int64) {
	for id, value := range t.entries {
		if value.expires <= now && value.sessions == 0 {
			delete(t.entries, id)
		}
	}
	for id, until := range t.revoked {
		if until <= now {
			delete(t.revoked, id)
		}
	}
}
