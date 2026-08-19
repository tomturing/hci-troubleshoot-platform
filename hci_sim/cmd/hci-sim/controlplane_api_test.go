package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
