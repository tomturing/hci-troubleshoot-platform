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
	SupportID            string `json:"support_id"`
	KBDRevision          int    `json:"kbd_revision"`
	KBDChecksum          string `json:"kbd_checksum"`
	SignalsDigest        string `json:"signals_digest"`
	ToolContractRevision string `json:"tool_contract_revision"`
	PolicyRevision       string `json:"policy_revision"`
}

type syntheticRoute struct {
	Keyword string
	Limit   string
}

// syntheticCatalog 是故意很小的白名单。扩大范围必须以 Signal schema、命令契约和
// 独立验证为依据，不能根据 support_id 猜测客户环境。
var syntheticCatalog = map[string]syntheticRoute{
	"27736": {Keyword: "设置集群IP失败", Limit: "100"},
	"34164": {Keyword: "新建虚拟机", Limit: "1"},
	// KBD23821 的真实 published Signal 使用 qkv_task 关键词“迁移虚拟机”，
	// 仅用于 positive-minimal 的信号契约验收，不代表真实迁移 Artifact。
	"23821": {Keyword: "迁移虚拟机", Limit: "1"},
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
	catalog, ok := syntheticCatalog[strings.TrimSpace(*supportID)]
	if !ok {
		return fmt.Errorf("SYNTHETIC_ROUTE_UNSUPPORTED: KBD %s 不在 positive-minimal 白名单中", *supportID)
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
		strings.TrimSpace(resolved.ToolContractRevision) == "" || strings.TrimSpace(resolved.PolicyRevision) == "" {
		return fmt.Errorf("capability_gap: C1 resolved 输入与请求 KBD %s 不一致或不完整", *supportID)
	}
	manifest := buildSyntheticManifest(resolved, catalog, *node, *container)
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
	connection := map[string]any{
		"test_run_id": testRunID, "scenario_id": scenarioID, "support_id": *supportID,
		"issued_at": now.Format(time.RFC3339), "expires_at": expires.Format(time.RFC3339),
		"ttl_seconds":  int64(expires.Sub(now).Seconds()),
		"kbd_revision": resolved.KBDRevision, "variant": "positive-minimal", "execution_mode": "sim-ssh",
		"bundle_digest": manifest.Bundle.Digest, "virtual_node_id": *node, "container": *container,
		"connection": map[string]any{"host": *connectionHost, "port": *connectionPort, "username": "sim", "auth_type": "lease", "password": token, "execution_mode": "sim-ssh", "test_run_id": testRunID},
		"synthetic":  true, "not_real_artifact": true, "facts_boundary": "仅验证 Signal 合约与 sim-ssh 路由；不代表真实 Artifact/真实 HCI E2E。",
		"manifest_path": manifestPath, "recommended_command": fmt.Sprintf("qkv_task --keyword %q --limit %s --is_failed true", catalog.Keyword, catalog.Limit),
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

func buildSyntheticManifest(resolved *resolvedKbd, route syntheticRoute, node, container string) fixture.Manifest {
	argv := []string{"qkv_task", "--keyword", route.Keyword, "--limit", route.Limit, "--is_failed", "true"}
	return fixture.Manifest{
		SchemaVersion: fixture.SchemaVersion,
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: resolved.SupportID, Revision: resolved.KBDRevision, Checksum: "sha256:" + strings.TrimPrefix(resolved.KBDChecksum, "sha256:")},
		Contracts:     fixture.Contracts{ToolRevision: resolved.ToolContractRevision, PolicyRevision: resolved.PolicyRevision},
		Variables:     map[string]string{"SYNTHETIC": "true", "FACTS_BOUNDARY": "signal-contract-only", "SIGNALS_DIGEST": resolved.SignalsDigest},
		Limits:        fixture.Limits{MaxRoutes: 1, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes:        []fixture.Route{{ID: "synthetic-" + resolved.SupportID + "-sig-001", SignalID: "sig_001", Variant: "positive-minimal", RouteKey: fixture.RouteKey{Tool: "qkv_task", AcquisitionKey: "qkv_task:--keyword", Argv: argv, Node: node, Container: container}, Result: fixture.ResultDef{ExitCode: 0, Stdout: fmt.Sprintf("{\"synthetic\":true,\"support_id\":%q,\"signal_id\":\"sig_001\",\"status\":\"failed\",\"keyword\":%q,\"records\":[{\"synthetic_record\":true}]}\n", resolved.SupportID, route.Keyword)}, Fault: fixture.FaultDef{Type: fixture.FaultNone}}},
	}
}
