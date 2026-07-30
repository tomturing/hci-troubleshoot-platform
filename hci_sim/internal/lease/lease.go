// Package lease 实现 hci-sim 场景租约的签发、校验和进程内配额。
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

const tokenPrefix = "htp1"

var rawURL = base64.RawURLEncoding

// Claims 是绑定一次仿真运行的最小不可变上下文。
type Claims struct {
	LeaseID             string `json:"lease_id"`
	TestRunID           string `json:"test_run_id"`
	ScenarioID          string `json:"scenario_id"`
	FixtureManifestHash string `json:"fixture_manifest_hash"`
	FixtureVariant      string `json:"fixture_variant"`
	VirtualNodeID       string `json:"virtual_node_id,omitempty"`
	ExecutionMode       string `json:"execution_mode"`
	IssuedAt            int64  `json:"issued_at"`
	ExpiresAt           int64  `json:"expires_at"`
	MaxSessions         int    `json:"max_sessions"`
	MaxCommands         int    `json:"max_commands"`
}

// IsToken 只识别协议前缀，不执行真实性校验。
func IsToken(value string) bool {
	return strings.HasPrefix(strings.TrimSpace(value), tokenPrefix+".")
}

// Sign 使用 HMAC-SHA256 签发紧凑租约，token 本身不应进入日志。
func Sign(secret []byte, claims Claims) (string, error) {
	if err := validateClaims(claims, time.Now()); err != nil {
		return "", err
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", fmt.Errorf("序列化租约失败: %w", err)
	}
	encoded := rawURL.EncodeToString(payload)
	unsigned := tokenPrefix + "." + encoded
	signature := sign(secret, unsigned)
	return unsigned + "." + rawURL.EncodeToString(signature), nil
}

// Validate 校验签名、时效、模式和 manifest 绑定。
func Validate(secret []byte, token, expectedManifestHash string, now time.Time) (Claims, error) {
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
	if expectedManifestHash != "" && claims.FixtureManifestHash != expectedManifestHash {
		return Claims{}, errors.New("场景租约绑定的 fixture manifest 已漂移")
	}
	return claims, nil
}

func validateClaims(claims Claims, now time.Time) error {
	if claims.LeaseID == "" || claims.TestRunID == "" || claims.ScenarioID == "" {
		return errors.New("场景租约缺少运行标识")
	}
	if claims.FixtureManifestHash == "" {
		return errors.New("场景租约缺少 fixture manifest hash")
	}
	if claims.ExecutionMode != "sim-ssh" {
		return errors.New("场景租约 execution_mode 必须为 sim-ssh")
	}
	if claims.FixtureVariant == "" {
		return errors.New("场景租约缺少 fixture variant")
	}
	if claims.IssuedAt <= 0 || claims.ExpiresAt <= claims.IssuedAt {
		return errors.New("场景租约时间范围无效")
	}
	if now.Unix() >= claims.ExpiresAt {
		return errors.New("场景租约已过期")
	}
	if claims.IssuedAt > now.Add(2*time.Minute).Unix() {
		return errors.New("场景租约签发时间超前")
	}
	if claims.MaxSessions < 1 || claims.MaxSessions > 256 {
		return errors.New("场景租约会话配额无效")
	}
	if claims.MaxCommands < 1 || claims.MaxCommands > 10000 {
		return errors.New("场景租约命令配额无效")
	}
	return nil
}

func sign(secret []byte, value string) []byte {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(value))
	return mac.Sum(nil)
}

// Tracker 在单副本 P0 内实施 lease 会话和命令配额；P1 将状态外置到 Redis/CAS。
type Tracker struct {
	mu      sync.Mutex
	entries map[string]*usage
}

type usage struct {
	sessions int
	commands int
	expires  int64
}

func NewTracker() *Tracker {
	return &Tracker{entries: make(map[string]*usage)}
}

// AcquireSession 占用一个 SSH connection 配额，返回幂等释放函数。
func (t *Tracker) AcquireSession(claims Claims) (func(), error) {
	t.mu.Lock()
	t.cleanupLocked(time.Now().Unix())
	u := t.entries[claims.LeaseID]
	if u == nil {
		u = &usage{expires: claims.ExpiresAt}
		t.entries[claims.LeaseID] = u
	}
	if u.sessions >= claims.MaxSessions {
		t.mu.Unlock()
		return nil, errors.New("场景租约 SSH 会话配额已耗尽")
	}
	u.sessions++
	t.mu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() {
			t.mu.Lock()
			if current := t.entries[claims.LeaseID]; current != nil && current.sessions > 0 {
				current.sessions--
			}
			t.mu.Unlock()
		})
	}, nil
}

// ConsumeCommand 原子消费一次命令配额。
func (t *Tracker) ConsumeCommand(claims Claims) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.cleanupLocked(time.Now().Unix())
	u := t.entries[claims.LeaseID]
	if u == nil {
		u = &usage{expires: claims.ExpiresAt}
		t.entries[claims.LeaseID] = u
	}
	if u.commands >= claims.MaxCommands {
		return errors.New("场景租约命令配额已耗尽")
	}
	u.commands++
	return nil
}

func (t *Tracker) cleanupLocked(now int64) {
	for id, value := range t.entries {
		if value.expires <= now && value.sessions == 0 {
			delete(t.entries, id)
		}
	}
}
