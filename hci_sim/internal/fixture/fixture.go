// Package fixture 加载已发布的 Manifest v2，并以精确 RouteKey 执行确定性路由。
package fixture

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"unicode"
)

const SchemaVersion = "2.0"

const (
	FaultNone        = "none"
	FaultNonzeroExit = "nonzero_exit"
	FaultPermission  = "permission"
	FaultTimeout     = "timeout"
	FaultDisconnect  = "disconnect"
	FaultTruncate    = "truncate"
)

// Manifest 只表示已经发布、可被 Runtime 读取的不可变 Bundle。
type Manifest struct {
	SchemaVersion string            `json:"schema_version"`
	Bundle        BundleRef         `json:"bundle"`
	KBD           KBDRef            `json:"kbd"`
	Contracts     Contracts         `json:"contracts"`
	Variables     map[string]string `json:"variables"`
	Limits        Limits            `json:"limits"`
	Routes        []Route           `json:"routes"`
}

type BundleRef struct {
	Digest    string `json:"digest"`
	Status    string `json:"status"`
	Signature string `json:"signature,omitempty"`
}

type KBDRef struct {
	SupportID string `json:"support_id"`
	Revision  int    `json:"revision"`
	Checksum  string `json:"checksum"`
}

type Contracts struct {
	ToolRevision   string `json:"tool_revision"`
	PolicyRevision string `json:"policy_revision"`
}

type Limits struct {
	MaxRoutes                int `json:"max_routes"`
	MaxOutputBytesPerCommand int `json:"max_output_bytes_per_command"`
	MaxBundleBytes           int `json:"max_bundle_bytes"`
}

type Route struct {
	ID       string    `json:"id"`
	SignalID string    `json:"signal_id,omitempty"`
	Variant  string    `json:"variant"`
	RouteKey RouteKey  `json:"route_key"`
	Result   ResultDef `json:"result"`
	Stream   StreamDef `json:"stream"`
	Fault    FaultDef  `json:"fault"`
}

// RouteKey 不允许打分、部分匹配或顺序依赖；所有字段共同决定唯一 Fixture。
type RouteKey struct {
	Tool           string   `json:"tool"`
	AcquisitionKey string   `json:"acquisition_key"`
	Argv           []string `json:"argv"`
	Node           string   `json:"node"`
	Container      string   `json:"container"`
}

type ResultDef struct {
	ExitCode int    `json:"exit_code"`
	Stdout   string `json:"stdout,omitempty"`
	Stderr   string `json:"stderr,omitempty"`
}

type StreamDef struct {
	ChunkBytes      int `json:"chunk_bytes,omitempty"`
	ChunkIntervalMS int `json:"chunk_interval_ms,omitempty"`
}

type FaultDef struct {
	Type     string `json:"type"`
	AfterMS  int    `json:"after_ms,omitempty"`
	MaxBytes int    `json:"max_bytes,omitempty"`
}

// Result 是已渲染的、可安全执行的一次路由结果。
type Result struct {
	FixtureID          string
	SignalID           string
	RouteKey           RouteKey
	CommandFingerprint string
	ExitCode           int
	Stdout             string
	Stderr             string
	ChunkBytes         int
	ChunkIntervalMS    int
	Fault              FaultDef
}

// Router 持有经过完整性、语义和歧义检查的已发布 Bundle。
type Router struct {
	manifest     Manifest
	manifestHash string
	index        map[string]Route
}

// Load 从只读文件加载 Manifest。Runtime 不读取草稿或编辑态数据库。
func Load(path string) (*Router, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取 fixture manifest 失败: %w", err)
	}
	return Parse(raw)
}

// Parse 使用 DisallowUnknownFields，避免配置拼写错误静默降级。
func Parse(raw []byte) (*Router, error) {
	var manifest Manifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("解析 fixture manifest 失败: %w", err)
	}
	if err := ensureEOF(decoder); err != nil {
		return nil, err
	}
	if err := validateManifest(raw, &manifest); err != nil {
		return nil, err
	}
	// 将可选字段在进入索引前规范化，避免校验局部副本导致运行时状态不同。
	for index := range manifest.Routes {
		if manifest.Routes[index].Fault.Type == "" {
			manifest.Routes[index].Fault.Type = FaultNone
		}
	}
	index := make(map[string]Route, len(manifest.Routes))
	for _, route := range manifest.Routes {
		key := routeIndexKey(route.Variant, route.RouteKey)
		if _, exists := index[key]; exists {
			return nil, fmt.Errorf("fixture route 歧义: variant=%s route_key=%s", route.Variant, displayRouteKey(route.RouteKey))
		}
		index[key] = route
	}
	sum := sha256.Sum256(raw)
	return &Router{manifest: manifest, manifestHash: fmt.Sprintf("sha256:%x", sum[:]), index: index}, nil
}

func ensureEOF(decoder *json.Decoder) error {
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("fixture manifest 包含多个 JSON 值")
		}
		return fmt.Errorf("fixture manifest 尾部无效: %w", err)
	}
	return nil
}

func validateManifest(raw []byte, manifest *Manifest) error {
	if manifest.SchemaVersion != SchemaVersion {
		return fmt.Errorf("不支持 fixture schema %q", manifest.SchemaVersion)
	}
	if manifest.Bundle.Status != "published" || manifest.Bundle.Digest == "" {
		return errors.New("Runtime 只接受带 digest 的 published bundle")
	}
	if manifest.KBD.SupportID == "" || manifest.KBD.Revision < 1 || manifest.KBD.Checksum == "" {
		return errors.New("fixture manifest 缺少不可变 KBD 引用")
	}
	if manifest.Contracts.ToolRevision == "" || manifest.Contracts.PolicyRevision == "" {
		return errors.New("fixture manifest 缺少 Tool 或 Policy revision")
	}
	if manifest.Limits.MaxRoutes < 1 || manifest.Limits.MaxRoutes > 10000 || len(manifest.Routes) > manifest.Limits.MaxRoutes {
		return errors.New("fixture manifest route 数量越界")
	}
	if manifest.Limits.MaxOutputBytesPerCommand < 1 || manifest.Limits.MaxOutputBytesPerCommand > 64*1024*1024 {
		return errors.New("fixture manifest per-command 输出限制无效")
	}
	if manifest.Limits.MaxBundleBytes < len(raw) || manifest.Limits.MaxBundleBytes > 64*1024*1024 {
		return errors.New("fixture manifest bundle 大小限制无效")
	}
	if got := ComputeBundleDigest(*manifest); got != manifest.Bundle.Digest {
		return fmt.Errorf("fixture bundle digest 不匹配: got=%s", got)
	}
	for _, route := range manifest.Routes {
		if err := validateRoute(route, manifest.Limits.MaxOutputBytesPerCommand); err != nil {
			return err
		}
	}
	return nil
}

func validateRoute(route Route, outputLimit int) error {
	if route.ID == "" || route.Variant == "" {
		return errors.New("fixture route 缺少 id 或 variant")
	}
	if route.RouteKey.Tool == "" || route.RouteKey.AcquisitionKey == "" || route.RouteKey.Node == "" || route.RouteKey.Container == "" || len(route.RouteKey.Argv) == 0 {
		return fmt.Errorf("fixture route %s 缺少完整 RouteKey", route.ID)
	}
	normalized, err := NormalizeArgv(route.RouteKey.Argv)
	if err != nil {
		return fmt.Errorf("fixture route %s argv 无效: %w", route.ID, err)
	}
	if !sameStrings(normalized, route.RouteKey.Argv) {
		return fmt.Errorf("fixture route %s argv 未规范化", route.ID)
	}
	if len(route.Result.Stdout)+len(route.Result.Stderr) > outputLimit {
		return fmt.Errorf("fixture route %s 输出超过 bundle 限制", route.ID)
	}
	if route.Stream.ChunkBytes < 0 || route.Stream.ChunkBytes > 1024*1024 || route.Stream.ChunkIntervalMS < 0 || route.Stream.ChunkIntervalMS > 60000 {
		return fmt.Errorf("fixture route %s stream 配置越界", route.ID)
	}
	switch route.Fault.Type {
	case "", FaultNone, FaultNonzeroExit, FaultPermission, FaultTimeout, FaultDisconnect, FaultTruncate:
	default:
		return fmt.Errorf("fixture route %s fault type 无效", route.ID)
	}
	if route.Fault.AfterMS < 0 || route.Fault.AfterMS > 300000 || route.Fault.MaxBytes < 0 || route.Fault.MaxBytes > outputLimit {
		return fmt.Errorf("fixture route %s fault 配置越界", route.ID)
	}
	return nil
}

func (r *Router) ManifestHash() string { return r.manifestHash }
func (r *Router) BundleDigest() string { return r.manifest.Bundle.Digest }
func (r *Router) KBD() KBDRef          { return r.manifest.KBD }
func (r *Router) Contracts() Contracts { return r.manifest.Contracts }
func (r *Router) OutputLimit() int     { return r.manifest.Limits.MaxOutputBytesPerCommand }

// Match 将受限 lexer 的 argv 和权威 Lease target 转为精确 RouteKey。
func (r *Router) Match(command, variant, node, container string) (Result, error) {
	argv, err := Lex(command)
	if err != nil {
		return Result{}, err
	}
	return r.MatchArgv(argv, variant, node, container)
}

func (r *Router) MatchArgv(argv []string, variant, node, container string) (Result, error) {
	normalized, err := NormalizeArgv(argv)
	if err != nil {
		return Result{}, err
	}
	if variant == "" || node == "" || container == "" {
		return Result{}, errors.New("policy_denied: 缺少 variant、node 或 container")
	}
	key := RouteKey{Tool: toolFor(normalized), AcquisitionKey: acquisitionFor(normalized), Argv: normalized, Node: node, Container: container}
	route, ok := r.index[routeIndexKey(variant, key)]
	if !ok {
		return Result{RouteKey: key, CommandFingerprint: Fingerprint(key)}, errors.New("fixture_not_found")
	}
	return Result{
		FixtureID: route.ID, SignalID: route.SignalID, RouteKey: key, CommandFingerprint: Fingerprint(key),
		ExitCode: route.Result.ExitCode, Stdout: render(route.Result.Stdout, r.manifest.Variables), Stderr: render(route.Result.Stderr, r.manifest.Variables),
		ChunkBytes: route.Stream.ChunkBytes, ChunkIntervalMS: route.Stream.ChunkIntervalMS, Fault: route.Fault,
	}, nil
}

// Lex 只实现命令传输所需的引号/转义子集，绝不执行 shell 语义。
func Lex(command string) ([]string, error) {
	if strings.TrimSpace(command) == "" {
		return nil, errors.New("policy_denied: command 为空")
	}
	if strings.ContainsAny(command, "\r\n") || strings.ContainsRune(command, '\x00') {
		return nil, errors.New("policy_denied: 禁止多行或 NUL 命令")
	}
	var argv []string
	var current strings.Builder
	var quote rune
	escaped := false
	flush := func() {
		if current.Len() > 0 {
			argv = append(argv, current.String())
			current.Reset()
		}
	}
	for _, ch := range command {
		if escaped {
			current.WriteRune(ch)
			escaped = false
			continue
		}
		if quote != 0 {
			if ch == quote {
				quote = 0
			} else if ch == '\\' && quote == '"' {
				escaped = true
			} else {
				current.WriteRune(ch)
			}
			continue
		}
		switch {
		case ch == '\\':
			escaped = true
		case ch == '\'' || ch == '"':
			quote = ch
		case unicode.IsSpace(ch):
			flush()
		case strings.ContainsRune(";|&`$<>(){}*?[]!", ch):
			return nil, fmt.Errorf("policy_denied: 禁止 shell 操作符 %q", ch)
		default:
			current.WriteRune(ch)
		}
	}
	if escaped || quote != 0 {
		return nil, errors.New("policy_denied: 命令引号或转义未闭合")
	}
	flush()
	if len(argv) == 0 {
		return nil, errors.New("policy_denied: command 为空")
	}
	return NormalizeArgv(argv)
}

// NormalizeArgv 固定 --key=value 与 --key value 的表示，同时保持位置参数顺序。
func NormalizeArgv(argv []string) ([]string, error) {
	if len(argv) == 0 {
		return nil, errors.New("argv 为空")
	}
	out := make([]string, 0, len(argv)+2)
	for _, arg := range argv {
		if arg == "" || strings.ContainsAny(arg, "\r\n\x00") {
			return nil, errors.New("argv 包含空值、换行或 NUL")
		}
		if strings.HasPrefix(arg, "--") {
			if key, value, found := strings.Cut(arg, "="); found {
				if key == "--" || value == "" {
					return nil, errors.New("长参数格式无效")
				}
				out = append(out, key, value)
				continue
			}
		}
		out = append(out, arg)
	}
	return out, nil
}

func CanonicalCommand(command string) (string, error) {
	argv, err := Lex(command)
	if err != nil {
		return "", err
	}
	return strings.Join(argv, "\x1f"), nil
}

func Fingerprint(key RouteKey) string {
	payload, _ := json.Marshal(struct {
		Tool, AcquisitionKey, Node, Container string
		Argv                                  []string
	}{key.Tool, key.AcquisitionKey, key.Node, key.Container, key.Argv})
	sum := sha256.Sum256(payload)
	return fmt.Sprintf("sha256:%x", sum[:])
}

// ComputeBundleDigest 对 Bundle digest/signature 字段清空后的 canonical JSON 计算摘要。
func ComputeBundleDigest(manifest Manifest) string {
	manifest.Bundle.Digest = ""
	manifest.Bundle.Signature = ""
	payload, _ := json.Marshal(manifest)
	sum := sha256.Sum256(payload)
	return fmt.Sprintf("sha256:%x", sum[:])
}

// DigestFromJSON 用于发布流水线在签名/写入前计算 Bundle digest，不接受多值 JSON。
func DigestFromJSON(raw []byte) (string, error) {
	var manifest Manifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return "", fmt.Errorf("解析 fixture manifest 失败: %w", err)
	}
	if err := ensureEOF(decoder); err != nil {
		return "", err
	}
	return ComputeBundleDigest(manifest), nil
}

func routeIndexKey(variant string, key RouteKey) string {
	return variant + "\x1e" + displayRouteKey(key)
}
func displayRouteKey(key RouteKey) string {
	return key.Tool + "\x1f" + key.AcquisitionKey + "\x1f" + strings.Join(key.Argv, "\x1f") + "\x1f" + key.Node + "\x1f" + key.Container
}
func sameStrings(left, right []string) bool {
	return len(left) == len(right) && func() bool {
		for i := range left {
			if left[i] != right[i] {
				return false
			}
		}
		return true
	}()
}

func toolFor(argv []string) string {
	if len(argv) == 0 {
		return ""
	}
	return argv[0]
}
func acquisitionFor(argv []string) string {
	if len(argv) < 2 {
		return argv[0]
	}
	return argv[0] + ":" + argv[1]
}

func render(value string, variables map[string]string) string {
	keys := make([]string, 0, len(variables))
	for key := range variables {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		value = strings.ReplaceAll(value, "{{"+key+"}}", variables[key])
	}
	return value
}
