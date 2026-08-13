package main

// bootstrap 把已冻结的 C1 capability 解析结果编译为一个明确标记 synthetic 的
// positive-minimal Bundle，并签发仅供 Custom UI/Terminal Bridge 使用的短时 Lease。
// 它不读取编辑态 KBD、Artifact 内容或任意 URL；未知/未 ready KBD 一律拒绝。

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
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
	Metadata             map[string]any   `json:"metadata"`
	VerificationContract map[string]any   `json:"verification_contract"`
	SyntheticRoutes      []syntheticRoute `json:"synthetic_routes"`
}

type syntheticRoute struct {
	SignalID          string           `json:"signal_id"`
	Tool              string           `json:"tool"`
	Argv              []string         `json:"argv"`
	ToolRevision      int              `json:"tool_revision"`
	ToolChecksum      string           `json:"tool_checksum"`
	RequiredVariables []string         `json:"required_variables"`
	Role              string           `json:"role"`
	Matcher           map[string]any   `json:"matcher"`
	Produces          []map[string]any `json:"produces"`
}

type scenarioProfile struct {
	SchemaVersion string                         `json:"schema_version"`
	SampleSuite   string                         `json:"sample_suite"`
	Variables     map[string]string              `json:"variables"`
	Cases         map[string]scenarioCaseProfile `json:"cases"`
}

type scenarioCaseProfile struct {
	Title              string                    `json:"title"`
	FaultDescription   string                    `json:"fault_description"`
	ProductVersion     string                    `json:"product_version"`
	Variables          map[string]string         `json:"variables"`
	Signals            map[string]scenarioSignal `json:"signals"`
	ExpectedConclusion string                    `json:"expected_conclusion"`
}

type scenarioSignal struct {
	PositiveOutput string `json:"positive_output"`
	NegativeOutput string `json:"negative_output"`
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
	profilePath := flags.String("scenario-profile", "", "测试专用场景画像 JSON（不包含命令）")
	variant := flags.String("variant", "positive", "场景变体：positive/negative/missing-evidence/command-failed/timeout/version-incompatible")
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
	profile, err := loadScenarioProfile(*profilePath, resolved)
	if err != nil {
		return err
	}
	manifest, err := buildScenarioManifest(resolved, profile, *node, *container, *variant)
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
	scenarioID := fmt.Sprintf("kbd-%s-%s", *supportID, *variant)
	expires := now.Add(*ttl)
	claims := lease.Claims{
		JTI: testRunID + "-1", LeaseID: "lease-" + testRunID, TestRunID: testRunID, ScenarioID: scenarioID,
		SupportID: *supportID, KBDRevision: resolved.KBDRevision, BundleDigest: manifest.Bundle.Digest,
		FixtureVariant: *variant, ToolContractRevision: resolved.ToolContractRevision, PolicyRevision: resolved.PolicyRevision,
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
	recommendedCommands := make([]string, 0, len(manifest.Routes))
	for _, route := range manifest.Routes {
		recommendedCommands = append(recommendedCommands, shellDisplay(route.RouteKey.Argv))
	}
	recommendedCommand := ""
	if len(recommendedCommands) > 0 {
		recommendedCommand = recommendedCommands[0]
	}
	connection := map[string]any{
		"test_run_id": testRunID, "scenario_id": scenarioID, "support_id": *supportID,
		"issued_at": now.Format(time.RFC3339), "expires_at": expires.Format(time.RFC3339),
		"ttl_seconds":  int64(expires.Sub(now).Seconds()),
		"kbd_revision": resolved.KBDRevision, "variant": *variant, "execution_mode": "sim-ssh",
		"bundle_digest": manifest.Bundle.Digest, "virtual_node_id": *node, "container": *container,
		"connection": map[string]any{"host": *connectionHost, "port": *connectionPort, "username": "sim", "auth_type": "lease", "password": token, "execution_mode": "sim-ssh", "test_run_id": testRunID},
		"synthetic":  true, "not_real_artifact": true, "facts_boundary": "验证已发布 KBD/Tool 派生的在线 SSH 与离线本机采集契约；合成输出不代表真实 HCI 数据。",
		"manifest_path": manifestPath, "recommended_command": recommendedCommand, "recommended_commands": recommendedCommands,
	}
	if profile != nil {
		if caseProfile, ok := profile.Cases[resolved.SupportID]; ok {
			connection["scenario"] = map[string]any{
				"title": caseProfile.Title, "fault_description": caseProfile.FaultDescription,
				"product_version": caseProfile.ProductVersion, "expected_conclusion": caseProfile.ExpectedConclusion,
			}
		}
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
	return buildScenarioManifest(resolved, nil, node, container, "positive-minimal")
}

func loadScenarioProfile(path string, resolved *resolvedKbd) (*scenarioProfile, error) {
	if strings.TrimSpace(path) == "" {
		return nil, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取场景画像失败: %w", err)
	}
	var profile scenarioProfile
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&profile); err != nil {
		return nil, fmt.Errorf("场景画像无效: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, errors.New("场景画像包含多余 JSON 内容")
	}
	if profile.SchemaVersion != "1.0" || profile.SampleSuite == "" {
		return nil, errors.New("场景画像缺少 schema_version=1.0 或 sample_suite")
	}
	if expected := strings.TrimSpace(fmt.Sprint(resolved.Metadata["sample_suite"])); expected != "" && expected != profile.SampleSuite {
		return nil, fmt.Errorf("场景画像 sample_suite=%s 与 KBD metadata=%s 不一致", profile.SampleSuite, expected)
	}
	caseProfile, ok := profile.Cases[resolved.SupportID]
	if !ok {
		return nil, fmt.Errorf("场景画像未定义已发布 KBD %s", resolved.SupportID)
	}
	routeSignals := make(map[string]bool, len(resolved.SyntheticRoutes))
	for _, route := range resolved.SyntheticRoutes {
		routeSignals[route.SignalID] = true
		if _, ok := caseProfile.Signals[route.SignalID]; !ok {
			return nil, fmt.Errorf("场景画像缺少 Signal %s 的输出", route.SignalID)
		}
	}
	for signalID := range caseProfile.Signals {
		if !routeSignals[signalID] {
			return nil, fmt.Errorf("场景画像包含已发布 KBD 不存在的 Signal %s", signalID)
		}
	}
	return &profile, nil
}

func buildScenarioManifest(resolved *resolvedKbd, profile *scenarioProfile, node, container, selectedVariant string) (fixture.Manifest, error) {
	allowedVariants := map[string]bool{"positive-minimal": true, "positive": true, "negative": true, "missing-evidence": true, "command-failed": true, "timeout": true, "version-incompatible": true}
	if !allowedVariants[selectedVariant] {
		return fixture.Manifest{}, fmt.Errorf("不支持的场景变体: %s", selectedVariant)
	}
	variables := map[string]string{"SYNTHETIC": "true", "FACTS_BOUNDARY": "signal-contract-only", "SIGNALS_DIGEST": resolved.SignalsDigest}
	var caseProfile scenarioCaseProfile
	if profile != nil {
		for key, value := range profile.Variables {
			variables[key] = value
		}
		caseProfile = profile.Cases[resolved.SupportID]
		for key, value := range caseProfile.Variables {
			variables[key] = value
		}
	}
	routes := make([]fixture.Route, 0, len(resolved.SyntheticRoutes))
	seen := make(map[string]struct{}, len(resolved.SyntheticRoutes))
	for index, route := range resolved.SyntheticRoutes {
		if strings.TrimSpace(route.SignalID) == "" || strings.TrimSpace(route.Tool) == "" || route.ToolRevision < 1 || strings.TrimSpace(route.ToolChecksum) == "" {
			return fixture.Manifest{}, fmt.Errorf("capability_gap: synthetic route %d 缺少 Signal 或 Tool 修订事实", index)
		}
		renderedArgv := make([]string, len(route.Argv))
		for argvIndex, value := range route.Argv {
			renderedArgv[argvIndex] = renderProfileVariables(value, variables)
			if strings.Contains(renderedArgv[argvIndex], "{{") || strings.Contains(renderedArgv[argvIndex], "}}") {
				return fixture.Manifest{}, fmt.Errorf("capability_gap: Signal %s 缺少场景变量: %s", route.SignalID, value)
			}
		}
		argv, err := fixture.NormalizeArgv(renderedArgv)
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
		result := fixture.ResultDef{ExitCode: 0, Stdout: fmt.Sprintf(
			"{\"synthetic\":true,\"support_id\":%q,\"signal_id\":%q,\"status\":\"matched\",\"records\":[{\"synthetic_record\":true}]}\n",
			resolved.SupportID, route.SignalID,
		)}
		fault := fixture.FaultDef{Type: fixture.FaultNone}
		if profile != nil {
			signalOutput := caseProfile.Signals[route.SignalID]
			result.Stdout = renderProfileVariables(signalOutput.PositiveOutput, variables)
			switch selectedVariant {
			case "negative":
				result.Stdout = renderProfileVariables(signalOutput.NegativeOutput, variables)
			case "missing-evidence":
				if route.Role == "must" {
					result.Stdout = ""
				}
			case "command-failed":
				result.ExitCode = 1
				result.Stdout = ""
				result.Stderr = "synthetic command failed\n"
				fault.Type = fixture.FaultNonzeroExit
			case "timeout":
				result.Stdout = ""
				fault.Type = fixture.FaultTimeout
			case "version-incompatible":
				result.ExitCode = 1
				result.Stdout = ""
				result.Stderr = "当前命令不支持场景产品版本 " + caseProfile.ProductVersion + "\n"
				fault.Type = fixture.FaultNonzeroExit
			}
			if strings.Contains(result.Stdout, "{{") || strings.Contains(result.Stdout, "}}") ||
				strings.Contains(result.Stderr, "{{") || strings.Contains(result.Stderr, "}}") {
				return fixture.Manifest{}, fmt.Errorf("capability_gap: Signal %s 的场景输出缺少变量", route.SignalID)
			}
		}
		routes = append(routes, fixture.Route{
			ID:           fmt.Sprintf("synthetic-%s-%03d", resolved.SupportID, index+1),
			SignalID:     route.SignalID,
			ToolRevision: route.ToolRevision,
			ToolChecksum: route.ToolChecksum,
			Variant:      selectedVariant,
			RouteKey:     fixture.RouteKey{Tool: argv[0], AcquisitionKey: acquisitionKey, Argv: argv, Node: node, Container: container},
			Result:       result,
			Fault:        fault,
		})
	}
	return fixture.Manifest{
		SchemaVersion: fixture.SchemaVersion,
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           fixture.KBDRef{SupportID: resolved.SupportID, Revision: resolved.KBDRevision, Checksum: "sha256:" + strings.TrimPrefix(resolved.KBDChecksum, "sha256:")},
		Contracts:     fixture.Contracts{ToolRevision: resolved.ToolContractRevision, PolicyRevision: resolved.PolicyRevision},
		Variables:     variables,
		Limits:        fixture.Limits{MaxRoutes: len(routes), MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes:        routes,
	}, nil
}

func renderProfileVariables(value string, variables map[string]string) string {
	keys := make([]string, 0, len(variables))
	for key := range variables {
		keys = append(keys, key)
	}
	for _, key := range keys {
		value = strings.ReplaceAll(value, "{{"+key+"}}", variables[key])
	}
	return value
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
