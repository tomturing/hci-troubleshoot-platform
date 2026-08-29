package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"hci_sim/internal/controlplane"
)

func TestBundleFactoryAPICompileReviseDualApproveAndPublish(t *testing.T) {
	t.Setenv("HCI_SIM_ALLOW_SYNTHETIC_PUBLISH", "false")
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)

	compileBody := map[string]any{"resolved": map[string]any{
		"support_id": "27123", "kbd_revision": 25, "kbd_checksum": "sha256:kbd",
		"signals_digest": "sha256:signals", "tool_contract_revision": "tool-r25", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{{
			"signal_id": "sig-1", "tool": "qfk_system", "argv": []string{"acli", "system", "ps"},
			"tool_revision": 25, "tool_checksum": "sha256:tool", "role": "context",
		}},
	}}
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, compileBody, "compiler", "compiler-service", http.StatusCreated)
	draft := compiled["bundle"].(map[string]any)
	parentDigest := draft["digest"].(string)
	manifest := draft["manifest"].(map[string]any)
	routes := manifest["routes"].([]any)
	result := routes[0].(map[string]any)["result"].(map[string]any)
	result["stdout"] = "expert corrected output\n"

	revised := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+parentDigest+"/revise", map[string]any{
		"manifest": manifest, "reason": "专家修正输出证据",
	}, "expert", "expert-editor", http.StatusCreated)
	child := revised["bundle"].(map[string]any)
	childDigest := child["digest"].(string)
	if childDigest == parentDigest || child["parent_bundle_digest"] != parentDigest {
		t.Fatalf("revised bundle=%+v", child)
	}

	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/validate", map[string]any{}, "compiler", "compiler-service", http.StatusOK)
	// 修改者不能审批自己的 revision。
	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/approve-expert", map[string]any{}, "expert", "expert-editor", http.StatusForbidden)
	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/approve-expert", map[string]any{}, "expert", "expert-approver", http.StatusOK)
	approved := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/approve-security", map[string]any{}, "security", "security-approver", http.StatusOK)
	if approved["bundle"].(map[string]any)["status"] != string(controlplane.BundleApproved) {
		t.Fatalf("dual approval did not approve bundle: %+v", approved)
	}
	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/publish", map[string]any{}, "publisher", "publisher-service", http.StatusConflict)
	t.Setenv("HCI_SIM_ALLOW_SYNTHETIC_PUBLISH", "true")
	published := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/publish", map[string]any{}, "publisher", "publisher-service", http.StatusOK)
	activation := published["runtime_activation"].(map[string]any)
	if published["bundle"].(map[string]any)["status"] != string(controlplane.BundlePublished) || activation["status"] != "pending" {
		t.Fatalf("published response=%+v", published)
	}
	publishedBundle := published["bundle"].(map[string]any)
	datasets := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"/"+childDigest+"/dry-run-datasets?signal_id=sig-1&source_type=fixture", nil, "expert", "expert-editor", http.StatusOK)
	items := datasets["datasets"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["payload"] != "expert corrected output\n" {
		t.Fatalf("published fixture datasets=%+v", datasets)
	}
	publishedManifest := publishedBundle["manifest"].(map[string]any)
	publishedManifest["routes"].([]any)[0].(map[string]any)["result"].(map[string]any)["stdout"] = "expert revised published fixture\n"
	forked := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/revise", map[string]any{
		"manifest": publishedManifest, "reason": "基于已发布版本修正 stdout",
	}, "expert", "published-editor", http.StatusCreated)
	if forked["bundle"].(map[string]any)["status"] != string(controlplane.BundleDraft) || forked["bundle"].(map[string]any)["parent_bundle_digest"] != childDigest {
		t.Fatalf("published fork=%+v", forked)
	}
}

func TestBundleFactoryAPICompilesKBDWithProducedSceneVariable(t *testing.T) {
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{
		"resolved": map[string]any{
			"support_id": "40061", "kbd_revision": 1, "kbd_checksum": "sha256:kbd-40061",
			"signals_digest": "sha256:signals-40061", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
			"synthetic_routes": []map[string]any{{
				"signal_id": "sig_003", "tool": "qfk_log",
				"argv":               []string{"acli", "log", "get", "-t", "{{END}}", "-H", "{{HOST}}"},
				"required_variables": []string{"END", "HOST"}, "tool_revision": 1, "tool_checksum": "sha256:tool",
			}},
		},
	}, "compiler", "compiler-service", http.StatusCreated)
	manifest := compiled["bundle"].(map[string]any)["manifest"].(map[string]any)
	variables := manifest["variables"].(map[string]any)
	if variables["END"] != "2026-01-01 00:00:00" || variables["HOST"] != "SIM-HCI-NODE-01" {
		t.Fatalf("synthetic variables=%v", variables)
	}
	route := manifest["routes"].([]any)[0].(map[string]any)
	argv := route["route_key"].(map[string]any)["argv"].([]any)
	if argv[4] != "2026-01-01 00:00:00" || argv[6] != "SIM-HCI-NODE-01" {
		t.Fatalf("rendered argv=%v", argv)
	}
}

func TestBundleFactoryAPIRetiresDraftWithoutPhysicalDeletion(t *testing.T) {
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{"resolved": map[string]any{
		"support_id": "40062", "kbd_revision": 1, "kbd_checksum": "sha256:kbd-40062",
		"signals_digest": "sha256:signals-40062", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{{
			"signal_id": "sig_004", "tool": "qfk_log", "argv": []string{"acli", "log", "get"},
			"tool_revision": 1, "tool_checksum": "sha256:tool",
		}},
	}}, "compiler", "compiler-service", http.StatusCreated)
	digest := compiled["bundle"].(map[string]any)["digest"].(string)
	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+digest+"/retire", map[string]any{}, "expert", "expert-editor", http.StatusOK)
	record := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"/"+digest, nil, "expert", "expert-editor", http.StatusOK)
	if record["bundle"].(map[string]any)["status"] != string(controlplane.BundleRetired) {
		t.Fatalf("retired record=%+v", record)
	}
	listed := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"?support_id=40062", nil, "expert", "expert-editor", http.StatusOK)
	if bundles := listed["bundles"].([]any); len(bundles) != 0 {
		t.Fatalf("retired bundle must not be listed: %+v", bundles)
	}
	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+digest+"/retire", map[string]any{}, "compiler", "compiler-service", http.StatusForbidden)

	// 再次请求生成 Draft，应成功重激活且列表可见
	recompiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{"resolved": map[string]any{
		"support_id": "40062", "kbd_revision": 1, "kbd_checksum": "sha256:kbd-40062",
		"signals_digest": "sha256:signals-40062", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{{
			"signal_id": "sig_004", "tool": "qfk_log", "argv": []string{"acli", "log", "get"},
			"tool_revision": 1, "tool_checksum": "sha256:tool",
		}},
	}}, "compiler", "compiler-service", http.StatusCreated)
	if recompiled["bundle"].(map[string]any)["status"] != string(controlplane.BundleDraft) {
		t.Fatalf("recompiled bundle must be draft: %+v", recompiled)
	}
	listedAfter := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"?support_id=40062", nil, "expert", "expert-editor", http.StatusOK)
	if bundles := listedAfter["bundles"].([]any); len(bundles) != 1 || bundles[0].(map[string]any)["digest"] != digest {
		t.Fatalf("recompiled bundle must be listed: %+v", bundles)
	}
}

func TestBundleFactoryAPIAppendVerificationAssetBindsImmutableKbd(t *testing.T) {
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{"resolved": map[string]any{
		"support_id": "41398", "kbd_revision": 1, "kbd_checksum": "sha256:kbd-41398",
		"signals_digest": "sha256:signals-41398", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{{
			"signal_id": "sig_003", "tool": "qfk_system", "argv": []string{"acli", "system", "date"},
			"tool_revision": 1, "tool_checksum": "sha256:tool",
		}},
	}}, "compiler", "compiler-service", http.StatusCreated)
	parentDigest := compiled["bundle"].(map[string]any)["digest"].(string)

	// 1. 跨 KBD 注入报错
	bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+parentDigest+"/verification-assets", map[string]any{
		"asset": map[string]any{
			"asset_id": "va-1", "support_id": "99999", "kbd_revision": 999, "signal_id": "sig_003",
			"scope": "qfk_execution_result", "source_type": "pasted", "payload": "2026-08-27\n",
			"result_status": "PASS", "config_revision": "sha256:cfg", "trace_id": "t-1",
		},
		"reason": "跨 KBD 注入尝试",
	}, "expert", "expert-editor", http.StatusConflict)

	// 2. 客户端传错误的内部 DB 自增 revision（如 6469），服务端自动绑定权威 parent manifest 的 revision=1 和 support_id=41398
	appended := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+parentDigest+"/verification-assets", map[string]any{
		"asset": map[string]any{
			"asset_id": "va-1", "support_id": "41398", "kbd_revision": 6469, "signal_id": "sig_003",
			"scope": "qfk_execution_result", "source_type": "pasted", "payload": "2026-08-27\n",
			"result_status": "PASS", "config_revision": "sha256:cfg", "trace_id": "t-1",
		},
		"reason": "保存 Signal 试运行验证资产",
	}, "expert", "expert-editor", http.StatusCreated)

	childManifest := appended["bundle"].(map[string]any)["manifest"].(map[string]any)
	assets := childManifest["verification_assets"].([]any)
	if len(assets) != 1 {
		t.Fatalf("expected 1 verification asset, got %+v", assets)
	}
	first := assets[0].(map[string]any)
	if first["support_id"] != "41398" || int(first["kbd_revision"].(float64)) != 1 {
		t.Fatalf("expected kbd_revision=1, got %+v", first)
	}
}

func bundleFactoryRequest(t *testing.T, handler http.Handler, method, path string, body any, role, actorID string, wantStatus int) map[string]any {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(method, path, bytes.NewReader(raw))
	request.Header.Set("Authorization", "Bearer test-control-token")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-HCI-Sim-Actor-Role", role)
	request.Header.Set("X-HCI-Sim-Actor-ID", actorID)
	request.Header.Set("X-Trace-ID", "trace-test-bundle-factory")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != wantStatus {
		t.Fatalf("%s %s status=%d want=%d body=%s", method, path, recorder.Code, wantStatus, recorder.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response
}

// TestReviseDraftStalensParentDraft 验证每次 appendVerificationAsset 后，
// 父 Draft 被自动降级为 stale，List() 始终只返回一个 status=draft 的 Bundle。
// 这是保证前端 saveToBundle 的 drafts.length > 1 检查永远不会被意外触发的关键路径。
func TestReviseDraftStalesParentDraft(t *testing.T) {
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)

	// 编译初始 Draft-A
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{"resolved": map[string]any{
		"support_id": "stale-test", "kbd_revision": 1, "kbd_checksum": "sha256:kbd-stale",
		"signals_digest": "sha256:sig-stale", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{{
			"signal_id": "sig_001", "tool": "qfk_system", "argv": []string{"acli", "system", "date"},
			"tool_revision": 1, "tool_checksum": "sha256:tool",
		}},
	}}, "compiler", "compiler-service", http.StatusCreated)
	draftA := compiled["bundle"].(map[string]any)["digest"].(string)

	assetBody := func(assetID string) map[string]any {
		return map[string]any{
			"asset": map[string]any{
				"asset_id": assetID, "support_id": "stale-test", "kbd_revision": 1,
				"signal_id": "sig_001", "scope": "qfk_execution_result",
				"source_type": "pasted", "payload": "Wed Aug 27 12:00:00 CST 2026\n",
				"result_status": "PASS", "config_revision": "sha256:cfg", "trace_id": "t-stale",
			},
			"reason": "Signal 试运行验证资产保存",
		}
	}

	// 第 1 次保存 → Draft-A 应变为 stale，Draft-B 是唯一 draft
	res1 := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+draftA+"/verification-assets", assetBody("va-001"), "expert", "expert-editor", http.StatusCreated)
	draftB := res1["bundle"].(map[string]any)["digest"].(string)
	if draftB == draftA {
		t.Fatal("第 1 次追加后 digest 应该变化")
	}
	listed1 := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"?support_id=stale-test", nil, "expert", "expert-editor", http.StatusOK)
	bundles1 := listed1["bundles"].([]any)
	drafts1 := 0
	for _, b := range bundles1 {
		if b.(map[string]any)["status"] == "draft" {
			drafts1++
		}
	}
	if drafts1 != 1 {
		t.Fatalf("第 1 次保存后应只有 1 个 draft，得到 %d 个（bundles=%+v）", drafts1, bundles1)
	}

	// 第 2 次保存（模拟 signal1 调整后重新试运行） → Draft-B 应变 stale，Draft-C 是唯一 draft
	res2 := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+draftB+"/verification-assets", assetBody("va-002"), "expert", "expert-editor", http.StatusCreated)
	draftC := res2["bundle"].(map[string]any)["digest"].(string)
	if draftC == draftB {
		t.Fatal("第 2 次追加后 digest 应该变化")
	}
	listed2 := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"?support_id=stale-test", nil, "expert", "expert-editor", http.StatusOK)
	bundles2 := listed2["bundles"].([]any)
	drafts2 := 0
	for _, b := range bundles2 {
		if b.(map[string]any)["status"] == "draft" {
			drafts2++
		}
	}
	if drafts2 != 1 {
		t.Fatalf("第 2 次保存后应只有 1 个 draft，得到 %d 个（bundles=%+v）", drafts2, bundles2)
	}
}

// TestThreeSignalsCompleteBundle 验证 KBD 有 3 个 Signal，分别调试保存后
// 最终 Draft 包含 3 个 Signal 的完整输出，可直接用于发布。
func TestThreeSignalsCompleteBundle(t *testing.T) {
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)

	// 编译初始 Draft（3 个 signal，stdout 均为空）
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{"resolved": map[string]any{
		"support_id": "kbd-3sig", "kbd_revision": 1, "kbd_checksum": "sha256:kbd-3sig",
		"signals_digest": "sha256:sig-3sig", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{
			{
				"signal_id": "sig_001", "tool": "qfk_system", "argv": []string{"acli", "system", "ps"},
				"tool_revision": 1, "tool_checksum": "sha256:tool",
			},
			{
				"signal_id": "sig_002", "tool": "qfk_system", "argv": []string{"acli", "system", "date"},
				"tool_revision": 1, "tool_checksum": "sha256:tool",
			},
			{
				"signal_id": "sig_003", "tool": "qfk_system", "argv": []string{"acli", "system", "uptime"},
				"tool_revision": 1, "tool_checksum": "sha256:tool",
			},
		},
	}}, "compiler", "compiler-service", http.StatusCreated)
	currentDigest := compiled["bundle"].(map[string]any)["digest"].(string)

	save := func(signalID, assetID, payload string) string {
		res := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+currentDigest+"/verification-assets", map[string]any{
			"asset": map[string]any{
				"asset_id": assetID, "support_id": "kbd-3sig", "kbd_revision": 1,
				"signal_id": signalID, "scope": "qfk_execution_result",
				"source_type": "pasted", "payload": payload,
				"result_status": "PASS", "config_revision": "sha256:cfg", "trace_id": "t-" + assetID,
			},
			"reason": signalID + " 试运行验证资产保存",
		}, "expert", "expert-editor", http.StatusCreated)
		return res["bundle"].(map[string]any)["digest"].(string)
	}

	// signal1: 调试 1 次即通过
	currentDigest = save("sig_001", "va-s1-1", "process list output\n")
	// signal2: 调试 3 次，只有第 3 次通过保存（前两次试运行不调用 save）
	currentDigest = save("sig_002", "va-s2-3", "Wed Aug 27 12:00:00 CST 2026\n")
	// signal3: 调试 1 次即通过
	currentDigest = save("sig_003", "va-s3-1", "up 3 days, 4:12\n")

	// 验证最终 Draft 只有一个，且 3 个 Signal 的 Route stdout 均已更新
	listed := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"?support_id=kbd-3sig", nil, "expert", "expert-editor", http.StatusOK)
	var draftCount int
	for _, b := range listed["bundles"].([]any) {
		if b.(map[string]any)["status"] == "draft" {
			draftCount++
		}
	}
	if draftCount != 1 {
		t.Fatalf("3 次保存后应只有 1 个 draft，得到 %d 个", draftCount)
	}
	final := bundleFactoryRequest(t, mux, http.MethodGet, controlPlanePrefix+"/"+currentDigest, nil, "expert", "expert-editor", http.StatusOK)
	routes := final["bundle"].(map[string]any)["manifest"].(map[string]any)["routes"].([]any)
	stdouts := map[string]string{}
	for _, r := range routes {
		rm := r.(map[string]any)
		stdouts[rm["signal_id"].(string)] = rm["result"].(map[string]any)["stdout"].(string)
	}
	if stdouts["sig_001"] != "process list output\n" || stdouts["sig_002"] != "Wed Aug 27 12:00:00 CST 2026\n" || stdouts["sig_003"] != "up 3 days, 4:12\n" {
		t.Fatalf("最终 Bundle 3 个 Signal 输出不完整: %+v", stdouts)
	}
}

func TestBundleFactoryAPIAppendVerificationAssetQKVUpdatesRouteStdout(t *testing.T) {
	registry := controlplane.NewMemoryRegistryWithDependencies(controlplane.NewMemoryArtifactRegistry(), controlplane.NewMemoryBundleObjectStore())
	mux := http.NewServeMux()
	registerControlPlaneAPI(mux, registry, "test-control-token", false)

	// 编译初始包含 QKV 信号的 Draft
	compiled := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix, map[string]any{"resolved": map[string]any{
		"support_id": "41446", "kbd_revision": 3, "kbd_checksum": "sha256:kbd-41446",
		"signals_digest": "sha256:signals-41446", "tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
		"synthetic_routes": []map[string]any{{
			"signal_id": "sig_001", "tool": "qkv_alert", "argv": []string{"acli", "alert", "get"},
			"tool_revision": 1, "tool_checksum": "sha256:tool",
		}},
	}}, "compiler", "compiler-service", http.StatusCreated)
	parentDigest := compiled["bundle"].(map[string]any)["digest"].(string)

	// 追加 QKV 验证资产（Scope: qkv_variable_processing，Payload: JSON 数组）
	qkvPayload := []map[string]any{
		{
			"alert_type":  "删除虚拟机",
			"description": "存在虚拟机，请迁移后再删除",
			"vm":          "7903385510955",
		},
	}
	appended := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+parentDigest+"/verification-assets", map[string]any{
		"asset": map[string]any{
			"asset_id": "va-qkv-001", "support_id": "41446", "kbd_revision": 3, "signal_id": "sig_001",
			"scope": "qkv_variable_processing", "source_type": "pasted", "payload": qkvPayload,
			"result_status": "PASS", "config_revision": "sha256:cfg", "trace_id": "trace-qkv-001",
		},
		"reason": "保存 QKV 试运行验证资产",
	}, "expert", "expert-editor", http.StatusCreated)

	childBundle := appended["bundle"].(map[string]any)
	newDigest := childBundle["digest"].(string)
	if newDigest == parentDigest {
		t.Fatalf("ReviseDraft 应产生新的 digest，但 parent 和 child 相同")
	}

	manifest := childBundle["manifest"].(map[string]any)
	routes := manifest["routes"].([]any)
	if len(routes) != 1 {
		t.Fatalf("expected 1 route, got %d", len(routes))
	}
	updatedRoute := routes[0].(map[string]any)
	updatedResult := updatedRoute["result"].(map[string]any)
	stdoutStr := updatedResult["stdout"].(string)

	if !strings.Contains(stdoutStr, "删除虚拟机") || !strings.Contains(stdoutStr, "7903385510955") {
		t.Fatalf("QKV Route stdout 未正确更新为 JSON payload: %s", stdoutStr)
	}

	// 验证 verification_assets 中已追加记录
	assets := manifest["verification_assets"].([]any)
	if len(assets) != 1 {
		t.Fatalf("expected 1 verification asset, got %d", len(assets))
	}
	firstAsset := assets[0].(map[string]any)
	if firstAsset["asset_id"] != "va-qkv-001" || firstAsset["scope"] != "qkv_variable_processing" {
		t.Fatalf("verification asset 记录不匹配: %+v", firstAsset)
	}
}
