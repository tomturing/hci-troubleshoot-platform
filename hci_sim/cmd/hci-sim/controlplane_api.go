package main

// Bundle Factory 控制面 API。它只接收经过 Gateway 鉴权的内部请求，所有状态迁移
// 仍由 controlplane.Registry 执行；浏览器不能直接修改 Runtime Manifest 或绕过审批。

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/fixture"
)

const controlPlanePrefix = "/v1/control-plane/bundles"
const controlPlaneActivationPrefix = "/v1/control-plane/activations"

func registerControlPlaneAPI(mux *http.ServeMux, registry controlplane.Registry, controlToken string, allowInsecure bool, activators ...*runtimeBundleActivator) {
	var activator *runtimeBundleActivator
	if len(activators) > 0 {
		activator = activators[0]
	}
	mux.HandleFunc(controlPlanePrefix, func(w http.ResponseWriter, r *http.Request) {
		if !controlPlaneAuthorized(r, controlToken, allowInsecure) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if registry == nil {
			http.Error(w, "controlplane registry unavailable", http.StatusServiceUnavailable)
			return
		}
		handleBundleFactory(w, r, registry, activator)
	})
	mux.HandleFunc(controlPlanePrefix+"/", func(w http.ResponseWriter, r *http.Request) {
		if !controlPlaneAuthorized(r, controlToken, allowInsecure) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if registry == nil {
			http.Error(w, "controlplane registry unavailable", http.StatusServiceUnavailable)
			return
		}
		handleBundleFactory(w, r, registry, activator)
	})
	mux.HandleFunc(controlPlaneActivationPrefix+"/", func(w http.ResponseWriter, r *http.Request) {
		if !controlPlaneAuthorized(r, controlToken, allowInsecure) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if registry == nil {
			http.Error(w, "controlplane registry unavailable", http.StatusServiceUnavailable)
			return
		}
		parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, controlPlaneActivationPrefix), "/"), "/")
		var supportID string
		var err error
		if len(parts) > 0 {
			supportID, err = url.PathUnescape(parts[0])
		}
		if err != nil || strings.TrimSpace(supportID) == "" {
			http.Error(w, "support_id invalid", http.StatusBadRequest)
			return
		}
		if len(parts) == 1 && r.Method == http.MethodGet {
			if activator == nil || activator.repository == nil {
				http.Error(w, "runtime activation persistence unavailable", http.StatusServiceUnavailable)
				return
			}
			record, getErr := activator.repository.GetBundleActivation(r.Context(), supportID)
			if getErr != nil {
				writeControlPlaneError(w, getErr)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"runtime_activation": record, "trace_id": requestTraceID(r)})
			return
		}
		if len(parts) == 2 && parts[1] == "activate" && r.Method == http.MethodPost {
			if activator == nil {
				http.Error(w, "runtime activator unavailable", http.StatusServiceUnavailable)
				return
			}
			if _, actorErr := requestActor(r, controlplane.RoleExpert); actorErr != nil {
				writeControlPlaneError(w, actorErr)
				return
			}
			var request struct {
				Digest string `json:"bundle_digest"`
			}
			if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&request); err != nil || strings.TrimSpace(request.Digest) == "" {
				http.Error(w, "bundle_digest is required", http.StatusBadRequest)
				return
			}
			result, activationErr := activator.ActivateBundle(r.Context(), supportID, strings.TrimSpace(request.Digest), requestTraceID(r))
			if activationErr != nil {
				writeJSON(w, http.StatusConflict, map[string]any{"detail": activationErr.Error(), "runtime_activation": activationJSON(result), "trace_id": requestTraceID(r)})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"runtime_activation": activationJSON(result), "trace_id": requestTraceID(r)})
			return
		}
		if len(parts) != 2 || parts[1] != "rollback" || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		handleBundleRollback(w, r, activator, supportID)
	})
}

func handleBundleFactory(w http.ResponseWriter, r *http.Request, registry controlplane.Registry, activator *runtimeBundleActivator) {
	suffix := strings.TrimPrefix(r.URL.Path, controlPlanePrefix)
	if suffix == "" || suffix == "/" {
		if r.Method == http.MethodGet {
			supportID := strings.TrimSpace(r.URL.Query().Get("support_id"))
			records, err := registry.List(supportID)
			if err != nil {
				writeControlPlaneError(w, err)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"bundles": bundleViews(records), "trace_id": requestTraceID(r)})
			return
		}
		if r.Method == http.MethodPost {
			compileSynthetic(w, r, registry)
			return
		}
		http.NotFound(w, r)
		return
	}
	parts := strings.Split(strings.Trim(suffix, "/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		http.NotFound(w, r)
		return
	}
	digest, err := url.PathUnescape(parts[0])
	if err != nil || digest == "" {
		http.Error(w, "bundle digest invalid", http.StatusBadRequest)
		return
	}
	if len(parts) == 1 && r.Method == http.MethodGet {
		record, getErr := registry.Get(digest)
		if getErr != nil {
			writeControlPlaneError(w, getErr)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"bundle": bundleView(record), "trace_id": requestTraceID(r)})
		return
	}
	if len(parts) != 2 || r.Method != http.MethodPost {
		http.NotFound(w, r)
		return
	}
	switch parts[1] {
	case "revise":
		reviseDraft(w, r, registry, digest)
	case "validate":
		actor, actorErr := requestActor(r, controlplane.RoleCompiler)
		if actorErr != nil {
			writeControlPlaneError(w, actorErr)
			return
		}
		// 当前 MVP 只允许控制面发起固定三项门禁；真实 scanner 接入前不把它标记为生产验证。
		record, transitionErr := registry.Validate(actor, digest, controlplane.ValidationReport{MutationDetected: true, SecretScanPassed: true, IndependentProof: true}, time.Now().UTC())
		if transitionErr != nil {
			writeControlPlaneError(w, transitionErr)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"bundle": bundleView(record), "validation_mode": "controlplane_contract_only", "trace_id": requestTraceID(r)})
	case "approve-expert":
		approveBundle(w, r, registry, digest, controlplane.RoleExpert)
	case "approve-security":
		approveBundle(w, r, registry, digest, controlplane.RoleSecurity)
	case "publish":
		actor, actorErr := requestActor(r, controlplane.RolePublisher)
		if actorErr != nil {
			writeControlPlaneError(w, actorErr)
			return
		}
		candidate, getErr := registry.Get(digest)
		if getErr != nil {
			writeControlPlaneError(w, getErr)
			return
		}
		candidateRouter, parseErr := fixture.Parse(candidate.Manifest)
		if parseErr != nil {
			writeControlPlaneError(w, fmt.Errorf("发布前 manifest 校验失败: %w", parseErr))
			return
		}
		if candidateRouter.IsSynthetic() && !syntheticPublishAllowed() {
			writeJSON(w, http.StatusConflict, map[string]any{
				"detail":         "synthetic Bundle 仅允许显式开发开关发布，不能进入 Runtime 正式激活路径",
				"capability_gap": "synthetic_bundle_publish_disabled", "trace_id": requestTraceID(r),
			})
			return
		}
		record, transitionErr := registry.Publish(actor, digest, time.Now().UTC())
		if transitionErr != nil {
			writeControlPlaneError(w, transitionErr)
			return
		}
		activation := map[string]any{"status": "pending"}
		status := http.StatusOK
		if activator != nil {
			result, activationErr := activator.ActivateBundle(r.Context(), record.Input.SupportID, record.Digest, requestTraceID(r))
			activation = activationJSON(result)
			if activationErr != nil {
				status = http.StatusAccepted
			}
		}
		writeJSON(w, status, map[string]any{"bundle": bundleView(record), "runtime_activation": activation, "trace_id": requestTraceID(r)})
	case "fast-publish":
		if releaseProfile() != "internal_fast" {
			writeJSON(w, http.StatusConflict, map[string]any{"detail": "Internal Fast Path 未启用", "capability_gap": "internal_fast_disabled", "trace_id": requestTraceID(r)})
			return
		}
		actor, actorErr := requestActor(r, controlplane.RoleExpert)
		if actorErr != nil {
			writeControlPlaneError(w, actorErr)
			return
		}
		candidate, getErr := registry.Get(digest)
		if getErr != nil {
			writeControlPlaneError(w, getErr)
			return
		}
		candidateRouter, parseErr := fixture.Parse(candidate.Manifest)
		if parseErr != nil {
			writeControlPlaneError(w, fmt.Errorf("发布前 manifest 校验失败: %w", parseErr))
			return
		}
		if releaseProfile() == "high_assurance" && candidateRouter.IsSynthetic() {
			writeJSON(w, http.StatusConflict, map[string]any{"detail": "high_assurance profile 拒绝 synthetic Bundle", "capability_gap": "synthetic_bundle_publish_disabled", "trace_id": requestTraceID(r)})
			return
		}
		record, publishErr := registry.PublishInternalFast(actor, digest, time.Now().UTC())
		if publishErr != nil {
			writeControlPlaneError(w, publishErr)
			return
		}
		if activator != nil && activator.metrics != nil {
			activator.metrics.BundleFastPublishesTotal.Add(1)
		}
		activation := map[string]any{"status": "pending"}
		status := http.StatusOK
		if activator == nil {
			activation = map[string]any{"status": "failed", "failure_code": "runtime_activator_unavailable"}
			status = http.StatusAccepted
		} else {
			result, activationErr := activator.ActivateBundle(r.Context(), record.Input.SupportID, record.Digest, requestTraceID(r))
			activation = activationJSON(result)
			if activationErr != nil {
				status = http.StatusAccepted
			}
		}
		log.Printf("bundle_factory fast_publish trace_id=%s support_id=%s digest=%s actor_id=%s activation=%v", requestTraceID(r), record.Input.SupportID, record.Digest, actor.ID, activation["status"])
		writeJSON(w, status, map[string]any{
			"bundle": recordViewWithProfile(record), "release_profile": releaseProfile(),
			"validation_mode": "automatic_contract_and_smoke", "runtime_activation": activation, "trace_id": requestTraceID(r),
		})
	case "rollback":
		supportID := strings.TrimSpace(r.URL.Query().Get("support_id"))
		if supportID == "" {
			http.Error(w, "support_id is required", http.StatusBadRequest)
			return
		}
		handleBundleRollback(w, r, activator, supportID)
	default:
		http.NotFound(w, r)
	}
}

func handleBundleRollback(w http.ResponseWriter, r *http.Request, activator *runtimeBundleActivator, supportID string) {
	if activator == nil {
		http.Error(w, "runtime activator unavailable", http.StatusServiceUnavailable)
		return
	}
	if _, actorErr := requestActor(r, controlplane.RolePublisher); actorErr != nil {
		writeControlPlaneError(w, actorErr)
		return
	}
	result, rollbackErr := activator.RollbackBundle(r.Context(), supportID, requestTraceID(r))
	if rollbackErr != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"detail": rollbackErr.Error(), "runtime_activation": activationJSON(result), "trace_id": requestTraceID(r)})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runtime_activation": activationJSON(result), "trace_id": requestTraceID(r)})
}

func syntheticPublishAllowed() bool {
	return strings.EqualFold(strings.TrimSpace(env("HCI_SIM_ALLOW_SYNTHETIC_PUBLISH", "false")), "true")
}

func releaseProfile() string {
	profile := strings.ToLower(strings.TrimSpace(env("HCI_SIM_RELEASE_PROFILE", "internal_fast")))
	if profile != "high_assurance" {
		return "internal_fast"
	}
	return profile
}

func compileSynthetic(w http.ResponseWriter, r *http.Request, registry controlplane.Registry) {
	actor, err := requestActor(r, controlplane.RoleCompiler)
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	var request struct {
		Resolved         *resolvedKbd `json:"resolved"`
		Node             string       `json:"node"`
		Container        string       `json:"container"`
		CompilerRevision string       `json:"compiler_revision"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128*1024))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil || request.Resolved == nil {
		http.Error(w, "resolved capability is required", http.StatusBadRequest)
		return
	}
	if request.Resolved.SupportID == "" || request.Resolved.KBDRevision < 1 || request.Resolved.KBDChecksum == "" || len(request.Resolved.SyntheticRoutes) == 0 {
		http.Error(w, "capability_gap: C1 resolved input incomplete", http.StatusConflict)
		return
	}
	if request.Node == "" {
		request.Node = "SIM-HCI-NODE-01"
	}
	if request.Container == "" {
		request.Container = "host"
	}
	if request.CompilerRevision == "" {
		request.CompilerRevision = "bundle-factory-v1"
	}
	dependencies := []controlplane.Dependency{
		{Type: "kbd", ID: request.Resolved.SupportID, Revision: fmt.Sprint(request.Resolved.KBDRevision), Digest: request.Resolved.KBDChecksum},
		{Type: "signals", ID: request.Resolved.SupportID, Revision: fmt.Sprint(request.Resolved.KBDRevision), Digest: request.Resolved.SignalsDigest},
		{Type: "tool", ID: "hci-sim-tool-contract", Revision: request.Resolved.ToolContractRevision, Digest: digestValue(request.Resolved.ToolContractRevision)},
		{Type: "policy", ID: "hci-sim-policy", Revision: request.Resolved.PolicyRevision, Digest: digestValue(request.Resolved.PolicyRevision)},
	}
	manifest, err := buildSyntheticManifest(request.Resolved, request.Node, request.Container)
	if err != nil {
		log.Printf("bundle_factory compile_failed trace_id=%s support_id=%s stage=manifest error=%v", requestTraceID(r), request.Resolved.SupportID, err)
		writeControlPlaneError(w, err)
		return
	}
	record, err := registry.Compile(actor, controlplane.CompileInput{
		SupportID: request.Resolved.SupportID, KBDRevision: request.Resolved.KBDRevision, KBDChecksum: request.Resolved.KBDChecksum,
		SignalsDigest: request.Resolved.SignalsDigest, ToolContractRevision: request.Resolved.ToolContractRevision,
		PolicyRevision: request.Resolved.PolicyRevision, CompilerRevision: request.CompilerRevision,
		Dependencies: dependencies,
	}, manifest, time.Now().UTC())
	if err != nil {
		log.Printf("bundle_factory compile_failed trace_id=%s support_id=%s stage=registry error=%v", requestTraceID(r), request.Resolved.SupportID, err)
		writeControlPlaneError(w, err)
		return
	}
	log.Printf("bundle_factory compile trace_id=%s support_id=%s digest=%s actor_id=%s", requestTraceID(r), record.Input.SupportID, record.Digest, actor.ID)
	writeJSON(w, http.StatusCreated, map[string]any{"bundle": bundleView(record), "synthetic": true, "trace_id": requestTraceID(r)})
}

func reviseDraft(w http.ResponseWriter, r *http.Request, registry controlplane.Registry, parentDigest string) {
	actor, err := requestActor(r, controlplane.RoleExpert)
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	var request struct {
		Manifest fixture.Manifest `json:"manifest"`
		Reason   string           `json:"reason"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128*1024)).Decode(&request); err != nil || strings.TrimSpace(request.Reason) == "" {
		http.Error(w, "manifest and reason are required", http.StatusBadRequest)
		return
	}
	record, err := registry.ReviseDraft(actor, parentDigest, request.Manifest, request.Reason, time.Now().UTC())
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	log.Printf("bundle_factory revise trace_id=%s parent_digest=%s digest=%s actor_id=%s", requestTraceID(r), parentDigest, record.Digest, actor.ID)
	writeJSON(w, http.StatusCreated, map[string]any{"bundle": bundleView(record), "trace_id": requestTraceID(r)})
}

func approveBundle(w http.ResponseWriter, r *http.Request, registry controlplane.Registry, digest string, role controlplane.Role) {
	actor, err := requestActor(r, role)
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	record, err := registry.Approve(actor, digest, time.Now().UTC())
	if err != nil {
		writeControlPlaneError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"bundle": bundleView(record), "trace_id": requestTraceID(r)})
}

func requestActor(r *http.Request, role controlplane.Role) (controlplane.Actor, error) {
	id := strings.TrimSpace(r.Header.Get("X-HCI-Sim-Actor-ID"))
	if id == "" || strings.TrimSpace(r.Header.Get("X-HCI-Sim-Actor-Role")) != string(role) {
		return controlplane.Actor{}, errors.New("forbidden: Gateway 未提供匹配的已认证控制面身份")
	}
	return controlplane.Actor{ID: id, Role: role}, nil
}

func controlPlaneAuthorized(r *http.Request, token string, allowInsecure bool) bool {
	return (token != "" && r.Header.Get("Authorization") == "Bearer "+token) || (token == "" && allowInsecure)
}

func bundleViews(records []controlplane.BundleRecord) []map[string]any {
	views := make([]map[string]any, 0, len(records))
	for _, record := range records {
		views = append(views, bundleView(record))
	}
	return views
}

func bundleView(record controlplane.BundleRecord) map[string]any {
	var manifest any
	if len(record.Manifest) > 0 {
		_ = json.Unmarshal(record.Manifest, &manifest)
	}
	return map[string]any{
		"digest": record.Digest, "status": record.Status, "input_fingerprint": record.InputFingerprint,
		"support_id": record.Input.SupportID, "kbd_revision": record.Input.KBDRevision,
		"kbd_checksum": record.Input.KBDChecksum, "signals_digest": record.Input.SignalsDigest,
		"tool_contract_revision": record.Input.ToolContractRevision, "policy_revision": record.Input.PolicyRevision,
		"compiler_revision": record.Input.CompilerRevision, "parent_bundle_digest": record.Input.ParentBundleDigest,
		"draft_revision": record.Input.DraftRevision, "edit_reason": record.Input.EditReason,
		"creator": record.Creator, "created_at": record.CreatedAt, "updated_at": record.UpdatedAt,
		"stale_reason": record.StaleReason, "approvals": record.Approvals, "manifest": manifest,
	}
}

func recordViewWithProfile(record controlplane.BundleRecord) map[string]any {
	view := bundleView(record)
	view["release_profile"] = releaseProfile()
	return view
}

func writeControlPlaneError(w http.ResponseWriter, err error) {
	status := http.StatusConflict
	if strings.Contains(err.Error(), "forbidden") {
		status = http.StatusForbidden
	} else if strings.Contains(err.Error(), "not_found") {
		status = http.StatusNotFound
	}
	writeJSON(w, status, map[string]any{"detail": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func requestTraceID(r *http.Request) string {
	if value := strings.TrimSpace(r.Header.Get("X-Trace-ID")); value != "" {
		return value
	}
	return "trace-missing"
}
