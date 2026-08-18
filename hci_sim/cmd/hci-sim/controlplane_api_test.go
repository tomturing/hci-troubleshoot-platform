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
	published := bundleFactoryRequest(t, mux, http.MethodPost, controlPlanePrefix+"/"+childDigest+"/publish", map[string]any{}, "publisher", "publisher-service", http.StatusOK)
	if published["bundle"].(map[string]any)["status"] != string(controlplane.BundlePublished) || published["runtime_activation"] != "pending_gitops_sync" {
		t.Fatalf("published response=%+v", published)
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
