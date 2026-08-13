package main

// bootstrap 把已冻结的 C1 capability 解析结果编译为一个明确标记 synthetic 的
// positive-minimal Bundle，并签发仅供 Custom UI/Terminal Bridge 使用的短时 Lease。
// 它不读取编辑态 KBD、Artifact 内容或任意 URL；未知/未 ready KBD 一律拒绝。

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
)

type capabilityResponse struct {
	SupportID string       `json:"support_id"`
	Status    string       `json:"status"`
	Resolved  *resolvedKbd `json:"resolved"`
	Gaps      []struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"capability_gaps"`
}

type resolvedKbd struct {
	SupportID            string           `json:"support_id"`
	KBDRevision          int              `json:"kbd_revision"`
	KBDChecksum          string           `json:"kbd_checksum"`
	SignalsDigest        string           `json:"signals_digest"`
	ToolContractRevision string           `json:"tool_contract_revision"`
	PolicyRevision       string           `json:"policy_revision"`
	SyntheticRoutes      []syntheticRoute `json:"synthetic_routes"`
}

type syntheticRoute struct {
	SignalID     string   `json:"signal_id"`
	Tool         string   `json:"tool"`
	Argv         []string `json:"argv"`
	ToolRevision int      `json:"tool_revision"`
	ToolChecksum string   `json:"tool_checksum"`
}

func runBootstrap(args []string) error {
	flags := flag.NewFlagSet("bootstrap", flag.ContinueOnError)
	supportID := flags.String("kbd-id", "", "KBD support_id")
	capabilitiesURL := flags.String("capabilities-url", env("HCI_SIM_CAPABILITIES_URL", ""), "C1 capability API URL")
	apiToken := flags.String("api-token", env("INTERNAL_API_TOKEN", ""), "C1 internal API token")
	outputDir := flags.String("output-dir", "./.hci-sim-run", "Bundle 与连接信息输出目录")
	connectionHost := flags.String("connection-host", "127.0.0.1", "Custom UI 连接地址")
	connectionPort := flags.Int("connection-port", 2222, "Custom UI SSH 端口")
	node := flags.String("virtual-node", "SIM-HCI-NODE-01", "虚拟节点 ID")
	container := flags.String("container", "host", "目标容器")
	secret := flags.String("lease-key", env("HCI_SIM_LEASE_HMAC_KEY", ""), "Lease HMAC key（至少 32 字节）")
	issuer := flags.String("lease-issuer", env("HCI_SIM_LEASE_ISSUER", "hci-platform"), "Lease issuer")
	audience := flags.String("lease-audience", env("HCI_SIM_LEASE_AUDIENCE", "hci-sim"), "Lease audience")
	ttl := flags.Duration("ttl", 15*time.Minute, "Lease TTL")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*supportID) == "" {
		return errors.New("必须指定 --kbd-id")
	}
	if len([]byte(*secret)) < 32 {
		return errors.New("--lease-key 或 HCI_SIM_LEASE_HMAC_KEY 至少需要 32 字节")
	}
	capability, err := fetchCapability(*capabilitiesURL, *apiToken, *supportID)
	if err != nil {
		return err
	}
	if capability.Status != "ready_for_artifact_binding" || capability.Resolved == nil {
		if len(capability.Gaps) > 0 {
			return fmt.Errorf("capability_gap: KBD %s: %s (%s)", *supportID, capability.Gaps[0].Message, capability.Gaps[0].Code)
		}
		return fmt.Errorf("capability_gap: KBD %s 尚未 ready_for_artifact_binding", *supportID)
	}
	resolved := capability.Resolved
	if resolved.SupportID != *supportID || resolved.KBDRevision < 1 || strings.TrimSpace(resolved.KBDChecksum) == "" || strings.TrimSpace(resolved.SignalsDigest) == "" ||
		strings.TrimSpace(resolved.ToolContractRevision) == "" || strings.TrimSpace(resolved.PolicyRevision) == "" || len(resolved.SyntheticRoutes) == 0 {
		return fmt.Errorf("capability_gap: C1 resolved 输入与请求 KBD %s 不一致或不完整", *supportID)
	}
	manifest, err := buildSyntheticManifest(resolved, *node, *container)
	if err != nil {
		return err
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	raw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化 Bundle 失败: %w", err)
	}
	if _, err := fixture.Parse(raw); err != nil {
		return fmt.Errorf("synthetic Bundle 校验失败: %w", err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	testRunID := fmt.Sprintf("run-%s-%d", *supportID, now.Unix())
	scenarioID := fmt.Sprintf("kbd-%s-positive-minimal", *supportID)
	expires := now.Add(*ttl)
	claims := lease.Claims{
		JTI: testRunID + "-1", LeaseID: "lease-" + testRunID, TestRunID: testRunID, ScenarioID: scenarioID,
		SupportID: *supportID, KBDRevision: resolved.KBDRevision, BundleDigest: manifest.Bundle.Digest,
		FixtureVariant: "positive-minimal", ToolContractRevision: resolved.ToolContractRevision, PolicyRevision: resolved.PolicyRevision,
		VirtualNodeID: *node, Container: *container, ExecutionMode: "sim-ssh", Issuer: *issuer, Audience: *audience,
		IssuedAt: now.Unix(), NotBefore: now.Unix(), ExpiresAt: expires.Unix(), RunDeadline: expires.Unix(),
		MaxSessions: 4, MaxCommands: 200, MaxOutputBytes: int64(manifest.Limits.MaxOutputBytesPerCommand),
	}
	token, err := lease.Sign([]byte(*secret), claims)
	if err != nil {
		return fmt.Errorf("签发 sim-ssh Lease 失败: %w", err)
	}
	if err := os.MkdirAll(*outputDir, 0700); err != nil {
		return err
	}
	manifestPath := filepath.Join(*outputDir, "fixture-manifest.json")
	if err := os.WriteFile(manifestPath, raw, 0600); err != nil {
		return err
	}
	recommendedCommands := make([]string, 0, len(resolved.SyntheticRoutes))
	for _, route := range resolved.SyntheticRoutes {
		recommendedCommands = append(recommendedCommands, shellDisplay(route.Argv))
	}
	recommendedCommand := ""
	if len(recommendedCommands) > 0 {
		recommendedCommand = recommendedCommands[0]
	}
	connection := map[string]any{
		"test_run_id": testRunID, "scenario_id": scenarioID, "support_id": *supportID,
		"issued_at": now.Format(time.RFC3339), "expires_at": expires.Format(time.RFC3339),
		"ttl_seconds":  int64(expires.Sub(now).Seconds()),
		"kbd_revision": resolved.KBDRevision, "variant": "positive-minimal", "execution_mode": "sim-ssh",
		"bundle_digest": manifest.Bundle.Digest, "virtual_node_id": *node, "container": *container,
		"connection": map[string]any{"host": *connectionHost, "port": *connectionPort, "username": "sim", "auth_type": "lease", "password": token, "execution_mode": "sim-ssh", "test_run_id": testRunID},
		"synthetic":  true, "not_real_artifact": true, "facts_boundary": "仅验证 Signal 合约与 sim-ssh 路由；不代表真实 Artifact/真实 HCI E2E。",
		"manifest_path": manifestPath, "recommended_command": recommendedCommand, "recommended_commands": recommendedCommands,
	}
	connectionRaw, err := json.MarshalIndent(connection, "", "  ")
	if err != nil {
		return err
	}
	connectionPath := filepath.Join(*outputDir, "connection.json")
	if err := os.WriteFile(connectionPath, connectionRaw, 0600); err != nil {
		return err
	}
	// stdout 是给启动脚本/UI 的一次性能力交接；不写应用日志。
	_, _ = os.Stdout.Write(append(connectionRaw, '\n'))
	return nil
}

func fetchCapability(baseURL, token, supportID string) (capabilityResponse, error) {
	if strings.TrimSpace(baseURL) == "" {
		return capabilityResponse{}, errors.New("必须提供 --capabilities-url 或 HCI_SIM_CAPABILITIES_URL；禁止脱离 C1 权威快照编译")
	}
	req, err := http.NewRequest(http.MethodGet, strings.TrimRight(baseURL, "/")+"/"+supportID, nil)
	if err != nil {
		return capabilityResponse{}, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return capabilityResponse{}, fmt.Errorf("读取 C1 capability 失败: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return capabilityResponse{}, fmt.Errorf("读取 C1 capability 失败: HTTP %d", resp.StatusCode)
	}
	var result capabilityResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return capabilityResponse{}, fmt.Errorf("C1 capability 响应无效: %w", err)
	}
	return result, nil
}

func buildSyntheticManifest(resolved *resolvedKbd, node, container string) (fixture.Manifest, error) {
	routes := make([]fixture.Route, 0, len(resolved.SyntheticRoutes))
	seen := make(map[string]struct{}, len(resolved.SyntheticRoutes))
	for index, route := range resolved.SyntheticRoutes {
		if strings.TrimSpace(route.SignalID) == "" || strings.TrimSpace(route.Tool) == "" || route.ToolRevision < 1 || strings.TrimSpace(route.ToolChecksum) == "" {
			return fixture.Manifest{}, fmt.Errorf("capability_gap: synthetic route %d 缺少 Signal 或 Tool 修订事实", index)
		}
		argv, err := fixture.NormalizeArgv(route.Argv)
		if err != nil {
			return fixture.Manifest{}, fmt.Errorf("capability_gap: Signal %s argv 无效: %w", route.SignalID, err)
		}
		if len(argv) == 0 || argv[0] != "acli" {
			return fixture.Manifest{}, fmt.Errorf("capability_gap: Signal %s 不是受控 aCLI 只读路由", route.SignalID)
		}
		key := strings.Join(argv, "\x1f")
		if _, exists := seen[key]; exists {
			return fixture.Manifest{}, fmt.Errorf("capability_gap: Signal %s 与其他 Signal 生成了重复 RouteKey", route.SignalID)
		}
		seen[key] = struct{}{}
		acquisitionKey := argv[0]
		if len(argv) > 1 {
			acquisitionKey += ":" + argv[1]
		}
		routes = append(routes, fixture.Route{
			ID:           fmt.Sprintf("synthetic-%s-%03d", resolved.SupportID, index+1),
			SignalID:     route.SignalID,
			ToolRevision: route.ToolRevision,
			ToolChecksum: route.ToolChecksum,
			Variant:      "positive-minimal",
			RouteKey:     fixture.RouteKey{Tool: argv[0], AcquisitionKey: acquisitionKey, Argv: argv, Node: node, Container: container},
			Result: fixture.ResultDef{ExitCode: 0, Stdout: fmt.Sprintf(
				"{\"synthetic\":true,\"support_id\":%q,\"signal_id\":%q,\"status\":\"matched\",\"records\":[{\"synthetic_record\":true}]}\n",
				resolved.SupportID, route.SignalID,
			)},
			Fault: fixture.FaultDef{Type: fixture.FaultNone},
		})
	}
	return fixture.Manifest{
		SchemaVersion: fixture.SchemaVersion,
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: resolved.SupportID, Revision: resolved.KBDRevision, Checksum: "sha256:" + strings.TrimPrefix(resolved.KBDChecksum, "sha256:")},
		Contracts:     fixture.Contracts{ToolRevision: resolved.ToolContractRevision, PolicyRevision: resolved.PolicyRevision},
		Variables:     map[string]string{"SYNTHETIC": "true", "FACTS_BOUNDARY": "signal-contract-only", "SIGNALS_DIGEST": resolved.SignalsDigest},
		Limits:        fixture.Limits{MaxRoutes: len(routes), MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes:        routes,
	}, nil
}

func shellDisplay(argv []string) string {
	parts := make([]string, 0, len(argv))
	for _, value := range argv {
		if value != "" && !strings.ContainsAny(value, " \t'\"") {
			parts = append(parts, value)
			continue
		}
		parts = append(parts, "'"+strings.ReplaceAll(value, "'", "'\\''")+"'")
	}
	return strings.Join(parts, " ")
}
