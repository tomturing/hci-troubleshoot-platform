package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"hci_sim/internal/fixture"
)

func batchListReport() map[string]any {
	return map[string]any{
		"total":         2,
		"status_counts": map[string]int{"ready_for_artifact_binding": 1, "capability_gap": 1},
		"gap_counts":    map[string]int{"KBD_NOT_PUBLISHED": 1},
		"results": []map[string]any{
			{
				"support_id": "90001", "status": "ready_for_artifact_binding",
				"metadata": map[string]any{"sample_suite": "suite-v1"},
				"resolved": map[string]any{
					"support_id": "90001", "kbd_id": 1, "kbd_revision": 2,
					"kbd_checksum": strings.Repeat("a", 64), "signals_digest": "sha256:signals",
					"tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
					"synthetic_routes": []map[string]any{{
						"signal_id": "sig-task", "tool": "qkv_task", "role": "must",
						"argv":          []string{"acli", "task", "get", "-k", "demo", "-l", "1"},
						"tool_revision": 3, "tool_checksum": "sha256:tool",
					}},
				},
				"capability_gaps": []any{},
			},
			{
				"support_id": "90002", "status": "capability_gap", "resolved": nil,
				"capability_gaps": []map[string]any{{"code": "KBD_NOT_PUBLISHED", "message": "KBD 未发布"}},
			},
		},
	}
}

func TestRunCompileBatchCompilesReadyAndSkipsGaps(t *testing.T) {
	var gotQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/capabilities" {
			t.Errorf("unexpected path %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		gotQuery = r.URL.Query().Get("sample_suite")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(batchListReport())
	}))
	defer server.Close()

	outputDir := t.TempDir()
	if err := runCompileBatch([]string{
		"--capabilities-url", server.URL, "--api-token", "token",
		"--output-dir", outputDir, "--sample-suite", "suite-v1",
	}); err != nil {
		t.Fatal(err)
	}
	if gotQuery != "suite-v1" {
		t.Fatalf("sample_suite filter not forwarded: %q", gotQuery)
	}

	manifestPath := filepath.Join(outputDir, "kbd-90001-synthetic-fixture-manifest.json")
	router, err := fixture.Load(manifestPath)
	if err != nil {
		t.Fatalf("compiled manifest must pass runtime validation: %v", err)
	}
	if router.KBD().SupportID != "90001" || router.KBD().Revision != 2 || !router.IsSynthetic() {
		t.Fatalf("unexpected manifest identity: %+v", router.KBD())
	}
	if routes := router.Routes(); len(routes) != 1 || routes[0].Variant != "positive-minimal" {
		t.Fatalf("unexpected routes: %+v", routes)
	}

	raw, err := os.ReadFile(filepath.Join(outputDir, "batch-report.json"))
	if err != nil {
		t.Fatal(err)
	}
	var report batchCompileReport
	if err := json.Unmarshal(raw, &report); err != nil {
		t.Fatal(err)
	}
	if len(report.Compiled) != 1 || report.Compiled[0].Digest != router.BundleDigest() {
		t.Fatalf("unexpected compiled entries: %+v", report.Compiled)
	}
	if len(report.Skipped) != 1 || report.Skipped[0].SupportID != "90002" ||
		len(report.Skipped[0].GapCodes) != 1 || report.Skipped[0].GapCodes[0] != "KBD_NOT_PUBLISHED" {
		t.Fatalf("unexpected skipped entries: %+v", report.Skipped)
	}
	if report.Total != 2 || report.OutputDir != outputDir {
		t.Fatalf("unexpected report header: %+v", report)
	}
}

func TestRunCompileBatchSkipsIncompleteResolvedInput(t *testing.T) {
	item := batchListReport()["results"].([]map[string]any)[0]
	resolved := item["resolved"].(map[string]any)
	delete(resolved, "synthetic_routes")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"total": 1, "status_counts": map[string]int{"ready_for_artifact_binding": 1},
			"gap_counts": map[string]int{}, "results": []map[string]any{item},
		})
	}))
	defer server.Close()

	outputDir := t.TempDir()
	if err := runCompileBatch([]string{"--capabilities-url", server.URL, "--api-token", "token", "--output-dir", outputDir}); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(outputDir, "batch-report.json"))
	if err != nil {
		t.Fatal(err)
	}
	var report batchCompileReport
	if err := json.Unmarshal(raw, &report); err != nil {
		t.Fatal(err)
	}
	if len(report.Compiled) != 0 || len(report.Skipped) != 1 || report.Skipped[0].Reason == "" {
		t.Fatalf("incomplete resolved input must be skipped: %+v", report)
	}
}

func TestRunCompileBatchCompilesPureQfkVarLocalOperation(t *testing.T) {
	item := map[string]any{
		"support_id": "qfk-var-only", "status": "ready_for_artifact_binding", "metadata": map[string]any{},
		"resolved": map[string]any{
			"support_id": "qfk-var-only", "kbd_id": 1, "kbd_revision": 1,
			"kbd_checksum": strings.Repeat("a", 64), "signals_digest": "sha256:signals",
			"tool_contract_revision": "tool-r1", "policy_revision": "policy-r1",
			"verification_contract": map[string]any{"variables": map[string]any{"DESCRIPTION": map[string]any{"type": "string"}}},
			"local_operations": []map[string]any{{
				"signal_id": "extract-percent", "tool": "qfk_var", "mode": "derive", "operation": "feature_extract",
				"args":               map[string]any{"mode": "derive", "operation": "feature_extract", "input": "{{DESCRIPTION}}", "target_variable": "percent.current", "value_type": "percentage", "cardinality": "exactly_one"},
				"required_variables": []string{"DESCRIPTION"}, "produces": []map[string]any{{"name": "CURRENT_PERCENT", "type": "number"}},
			}},
		},
		"capability_gaps": []any{},
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"total": 1, "status_counts": map[string]int{"ready_for_artifact_binding": 1},
			"gap_counts": map[string]int{}, "results": []map[string]any{item},
		})
	}))
	defer server.Close()

	outputDir := t.TempDir()
	if err := runCompileBatch([]string{"--capabilities-url", server.URL, "--api-token", "token", "--output-dir", outputDir}); err != nil {
		t.Fatal(err)
	}
	manifestPath := filepath.Join(outputDir, "kbd-qfk-var-only-synthetic-fixture-manifest.json")
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	var manifest fixture.Manifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	if len(manifest.Routes) != 0 || len(manifest.LocalOperations) != 1 {
		t.Fatalf("qfk_var 批量编译结果错误: %+v", manifest)
	}
}

func TestRunCompileBatchFailsClosedWithoutSnapshot(t *testing.T) {
	if err := runCompileBatch([]string{"--api-token", "token", "--output-dir", t.TempDir()}); err == nil || !strings.Contains(err.Error(), "C1") {
		t.Fatalf("expected fail-closed without capabilities URL, got %v", err)
	}
}
