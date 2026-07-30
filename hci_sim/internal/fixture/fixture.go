// Package fixture 加载不可变 Fixture Manifest，并执行安全、确定性的命令路由。
package fixture

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
)

const SchemaVersion = "1.0"

// Manifest 是 P0 FixtureManifest v1 的文件表示。
type Manifest struct {
	SchemaVersion string            `json:"schema_version"`
	KBD           KBDRef            `json:"kbd"`
	Variables     map[string]string `json:"variables"`
	Routes        []Route           `json:"routes"`
}

type KBDRef struct {
	SupportID string `json:"support_id"`
	Revision  int    `json:"revision"`
	Checksum  string `json:"checksum"`
}

type Route struct {
	ID         string `json:"id"`
	SignalID   string `json:"signal_id,omitempty"`
	Variant    string `json:"variant"`
	Pattern    string `json:"command_pattern"`
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout,omitempty"`
	Stderr     string `json:"stderr,omitempty"`
	DelayMS    int    `json:"delay_ms,omitempty"`
	ChunkBytes int    `json:"chunk_bytes,omitempty"`
}

// Result 是渲染后的确定性执行结果。
type Result struct {
	FixtureID          string
	SignalID           string
	CanonicalCommand   string
	CommandFingerprint string
	ExitCode           int
	Stdout             string
	Stderr             string
	DelayMS            int
	ChunkBytes         int
}

type compiledRoute struct {
	route Route
	re    *regexp.Regexp
}

// Router 持有已校验 manifest 和预编译命令规则。
type Router struct {
	manifest Manifest
	hash     string
	routes   []compiledRoute
}

// Load 从只读文件加载并校验 manifest，hash 对原始字节计算以支持内容寻址。
func Load(path string) (*Router, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取 fixture manifest 失败: %w", err)
	}
	return Parse(raw)
}

// Parse 供测试和嵌入场景使用。
func Parse(raw []byte) (*Router, error) {
	var manifest Manifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return nil, fmt.Errorf("解析 fixture manifest 失败: %w", err)
	}
	if manifest.SchemaVersion != SchemaVersion {
		return nil, fmt.Errorf("不支持 fixture schema %q", manifest.SchemaVersion)
	}
	if manifest.KBD.SupportID == "" || manifest.KBD.Revision < 1 || manifest.KBD.Checksum == "" {
		return nil, errors.New("fixture manifest 缺少不可变 KBD 引用")
	}
	if len(manifest.Routes) == 0 {
		return nil, errors.New("fixture manifest 没有命令路由")
	}
	seen := make(map[string]struct{}, len(manifest.Routes))
	compiled := make([]compiledRoute, 0, len(manifest.Routes))
	for _, route := range manifest.Routes {
		if route.ID == "" || route.Variant == "" || route.Pattern == "" {
			return nil, errors.New("fixture route 缺少 id、variant 或 command_pattern")
		}
		if _, exists := seen[route.ID]; exists {
			return nil, fmt.Errorf("fixture route id 重复: %s", route.ID)
		}
		seen[route.ID] = struct{}{}
		if route.DelayMS < 0 || route.DelayMS > 300000 {
			return nil, fmt.Errorf("fixture route %s delay_ms 越界", route.ID)
		}
		if route.ChunkBytes < 0 || route.ChunkBytes > 1024*1024 {
			return nil, fmt.Errorf("fixture route %s chunk_bytes 越界", route.ID)
		}
		re, err := regexp.Compile("^(?:" + route.Pattern + ")$")
		if err != nil {
			return nil, fmt.Errorf("fixture route %s 正则无效: %w", route.ID, err)
		}
		compiled = append(compiled, compiledRoute{route: route, re: re})
	}
	sum := sha256.Sum256(raw)
	return &Router{manifest: manifest, hash: fmt.Sprintf("sha256:%x", sum[:]), routes: compiled}, nil
}

func (r *Router) ManifestHash() string { return r.hash }

func (r *Router) KBD() KBDRef { return r.manifest.KBD }

// Match 执行 fail-closed 路由。正则来自已发布 manifest，用户命令本身不作为正则。
func (r *Router) Match(command, variant string) (Result, error) {
	canonical, err := CanonicalCommand(command)
	if err != nil {
		return Result{}, err
	}
	for _, candidate := range r.routes {
		if candidate.route.Variant != variant && candidate.route.Variant != "*" {
			continue
		}
		if !candidate.re.MatchString(canonical) {
			continue
		}
		return Result{
			FixtureID: candidate.route.ID, SignalID: candidate.route.SignalID,
			CanonicalCommand: canonical, CommandFingerprint: Fingerprint(canonical),
			ExitCode: candidate.route.ExitCode,
			Stdout:   render(candidate.route.Stdout, r.manifest.Variables),
			Stderr:   render(candidate.route.Stderr, r.manifest.Variables),
			DelayMS:  candidate.route.DelayMS, ChunkBytes: candidate.route.ChunkBytes,
		}, nil
	}
	return Result{CanonicalCommand: canonical, CommandFingerprint: Fingerprint(canonical)}, errors.New("fixture_not_found")
}

// CanonicalCommand 只接受单条命令，禁止任意 shell/pipeline 注入。
func CanonicalCommand(command string) (string, error) {
	command = strings.TrimSpace(strings.ReplaceAll(command, "\r", ""))
	if command == "" {
		return "", errors.New("policy_denied: command 为空")
	}
	if strings.Contains(command, "\n") || strings.ContainsRune(command, '\x00') {
		return "", errors.New("policy_denied: 禁止多行或 NUL 命令")
	}
	blocked := []string{";", "&&", "||", "|", "`", "$(`", ">", "<"}
	for _, token := range blocked {
		if strings.Contains(command, token) {
			return "", fmt.Errorf("policy_denied: 禁止 shell 操作符 %q", token)
		}
	}
	return strings.Join(strings.Fields(command), " "), nil
}

func Fingerprint(canonical string) string {
	sum := sha256.Sum256([]byte(canonical))
	return fmt.Sprintf("sha256:%x", sum[:])
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
