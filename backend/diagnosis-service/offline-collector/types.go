package main

import (
	"encoding/json"
	"fmt"
	"regexp"
)

const (
	exitInternal   = 1
	exitUsage      = 2
	exitPreflight  = 3
	exitVerify     = 4
	exitDeclined   = 5
	exitCollection = 6
	exitPackaging  = 7
	magicV1        = "HCIEB1\n"
	magicV2        = "HCIEB2\n"
	magic          = magicV1
)

// vm 控制台采集专用执行器名称（qkv_vm_console 离线路径）。
// 该执行器不走通用只读命令白名单，只接受签名制品内冻结的结构化采集意图。
const executorVmConsoleCapture = "vm_console_capture"

const (
	// 采集意图操作版本：当前仅 v1，其他取值一律 fail-closed 拒绝。
	captureOperationVersionV1 = "v1"
	// 采集模式：基线截图 + 近黑时经人工确认后唤醒重截，不提供其他模式。
	captureModeBaselineThenPromptIfNearBlack = "baseline_then_prompt_if_near_black"
	// 采集意图超时上限（秒）：控制台截图是快速失败型采集，有意严于通用 1-3600 上限。
	maxCaptureIntentTimeoutSeconds = 60
	// vm_console_capture 专用输出上限（16MiB），与设计文档 max_capture_bytes 对齐；
	// 其他执行器保持 4MiB 上限（见 verify.go）。
	maxVmConsoleCaptureBytes int64 = 16 * 1024 * 1024
)

// 唤醒决定五值词表（与 manifest/capture-result.json 契约一致）。
const (
	wakeDecisionNotNeeded      = "not_needed"      // 基线不近黑，无需唤醒
	wakeDecisionConfirmed      = "confirmed"       // 操作员在 TTY 明确确认唤醒
	wakeDecisionDeclined       = "declined"        // 操作员拒绝唤醒
	wakeDecisionNonInteractive = "non_interactive" // 非交互模式或无交互 TTY，默认拒绝
	wakeDecisionTimedOut       = "timed_out"       // 确认输入超时，按拒绝处理
)

// 唤醒执行结果词表。
const (
	wakeResultNotAttempted    = "not_attempted"    // 未尝试唤醒（不需要/拒绝/非交互/超时）
	wakeResultSuccess         = "success"          // sendkey down 与重截图均成功
	wakeResultSendkeyFailed   = "sendkey_failed"   // 固定 sendkey down 操作失败
	wakeResultRecaptureFailed = "recapture_failed" // 唤醒后重截图失败
)

// hostNodeIDPattern 限定已验证节点标识字符集：仅字母/数字开头，后跟字母、数字、
// 点、下划线、连字符，最长 128 字符。任何 Shell 控制字符都会在此被拒绝，
// 从源头杜绝命令拼接风险。
var hostNodeIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// vmIDPattern 限定 VMID 为 1-20 位纯数字。
var vmIDPattern = regexp.MustCompile(`^[0-9]{1,20}$`)

// captureIntent 是签名制品中冻结的虚拟机控制台采集意图（execution_spec）。
// 字段与 diagnosis-service 签发契约一一对应；Go 运行时对每一项做受限值校验，
// 任一校验失败均拒绝执行（fail-closed），绝不降级为自由命令执行。
type captureIntent struct {
	Executor         string
	OperationVersion string
	CaptureMode      string
	HostNodeID       string
	VMID             string
	TimeoutSeconds   int
	MaxCaptureBytes  int64
	SourceSignalRefs []sourceSignalRef
}

// parseCaptureIntent 从结构化采集项还原并校验采集意图。
// 该校验在 verify 阶段执行一次（契约不合法直接拒绝制品），
// 在执行阶段仍会防御性复核，确保运行期绝不执行非法意图。
func parseCaptureIntent(item collectorExecutionItem) (captureIntent, error) {
	intent := captureIntent{
		Executor:         item.Executor,
		OperationVersion: item.OperationVersion,
		CaptureMode:      item.CaptureMode,
		HostNodeID:       item.HostNodeID,
		VMID:             item.VMID,
		TimeoutSeconds:   item.TimeoutSeconds,
		MaxCaptureBytes:  item.MaxCaptureBytes,
		SourceSignalRefs: item.SourceSignalRefs,
	}
	if intent.Executor != executorVmConsoleCapture {
		return captureIntent{}, fmt.Errorf("执行器必须是 %s", executorVmConsoleCapture)
	}
	if intent.OperationVersion != captureOperationVersionV1 {
		return captureIntent{}, fmt.Errorf("operation_version 仅支持 %s，实际：%q", captureOperationVersionV1, intent.OperationVersion)
	}
	if intent.CaptureMode != captureModeBaselineThenPromptIfNearBlack {
		return captureIntent{}, fmt.Errorf("capture_mode 仅支持 %s，实际：%q", captureModeBaselineThenPromptIfNearBlack, intent.CaptureMode)
	}
	if !hostNodeIDPattern.MatchString(intent.HostNodeID) {
		return captureIntent{}, fmt.Errorf("host_node_id 含非法字符或超长（仅允许字母/数字开头，后跟字母、数字、点、下划线、连字符，最长 128 字符）")
	}
	if !vmIDPattern.MatchString(intent.VMID) {
		return captureIntent{}, fmt.Errorf("vm_id 必须是 1-20 位纯数字，实际：%q", intent.VMID)
	}
	if intent.TimeoutSeconds < 1 || intent.TimeoutSeconds > maxCaptureIntentTimeoutSeconds {
		return captureIntent{}, fmt.Errorf("timeout_seconds 必须在 1-%d 秒之间，实际：%d", maxCaptureIntentTimeoutSeconds, intent.TimeoutSeconds)
	}
	if intent.MaxCaptureBytes < 1 || intent.MaxCaptureBytes > maxVmConsoleCaptureBytes {
		return captureIntent{}, fmt.Errorf("max_capture_bytes 必须在 1-%d 字节之间，实际：%d", maxVmConsoleCaptureBytes, intent.MaxCaptureBytes)
	}
	return intent, nil
}

type documentSignature struct {
	Algorithm            string `json:"algorithm"`
	KeyID                string `json:"key_id"`
	SignatureBase64      string `json:"signature_base64"`
	PublicKeyFingerprint string `json:"public_key_fingerprint"`
}

type artifactSignature struct {
	Algorithm            string `json:"algorithm"`
	KeyID                string `json:"key_id"`
	SignatureBase64      string `json:"signature_base64"`
	PublicKeyBase64      string `json:"public_key_base64"`
	PublicKeyFingerprint string `json:"public_key_fingerprint"`
	SignedAt             string `json:"signed_at"`
	ExpiresAt            string `json:"expires_at"`
}

type timeWindow struct {
	StartTime string `json:"start_time"`
	EndTime   string `json:"end_time"`
}

type outputContract struct {
	OutputPath string `json:"output_path"`
	MediaType  string `json:"media_type"`
}

type artifactItem struct {
	PlanItemID        string          `json:"plan_item_id"`
	CollectorID       string          `json:"collector_id"`
	CollectorRevision int             `json:"collector_revision"`
	CollectorChecksum string          `json:"collector_checksum"`
	Executor          string          `json:"executor"`
	Target            json.RawMessage `json:"target"`
	TimeWindow        timeWindow      `json:"time_window"`
	OutputContract    outputContract  `json:"output_contract"`
}

type artifactProfile struct {
	Name     string `json:"name"`
	Revision int    `json:"revision"`
	Version  string `json:"version"`
	Checksum string `json:"checksum"`
}

type bundleEncryption struct {
	Algorithm          string `json:"algorithm"`
	KeyWrapAlgorithm   string `json:"key_wrap_algorithm"`
	KeyID              string `json:"key_id"`
	PublicKeyPEMBase64 string `json:"public_key_pem_base64"`
	Format             string `json:"format"`
}

type artifactManifest struct {
	SchemaVersion    string            `json:"schema_version"`
	ArtifactType     string            `json:"artifact_type"`
	ArtifactID       string            `json:"artifact_id"`
	SessionID        string            `json:"session_id"`
	CollectionPlanID string            `json:"collection_plan_id"`
	TargetKey        string            `json:"target_key"`
	FileName         string            `json:"file_name"`
	Profile          artifactProfile   `json:"profile"`
	BundleEncryption *bundleEncryption `json:"bundle_encryption"`
	ArtifactSHA256   string            `json:"artifact_sha256"`
	Signature        artifactSignature `json:"signature"`
	CollectionItems  []artifactItem    `json:"collection_items"`
}

type collectorArtifact struct {
	SchemaVersion    string                   `json:"schema_version"`
	ArtifactID       string                   `json:"artifact_id"`
	SessionID        string                   `json:"session_id"`
	CollectionPlanID string                   `json:"collection_plan_id"`
	TargetKey        string                   `json:"target_key"`
	ExecutionItems   []collectorExecutionItem `json:"execution_items"`
}

type collectorExecutionItem struct {
	ItemID            string            `json:"item_id"`
	CollectorID       string            `json:"collector_id"`
	SourceSignalRefs  []sourceSignalRef `json:"source_signal_refs,omitempty"`
	CollectorRevision int               `json:"collector_revision"`
	CollectorChecksum string            `json:"collector_checksum"`
	Executor          string            `json:"executor"`
	TimeoutSeconds    int               `json:"timeout_seconds"`
	MaxOutputBytes    int64             `json:"max_output_bytes"`
	Argv              []string          `json:"argv,omitempty"`
	Method            string            `json:"method,omitempty"`
	Path              string            `json:"path,omitempty"`
	Guide             string            `json:"guide,omitempty"`
	// 以下字段仅 vm_console_capture 执行器使用，均在签名原文内冻结。
	OperationVersion string `json:"operation_version,omitempty"`
	CaptureMode      string `json:"capture_mode,omitempty"`
	HostNodeID       string `json:"host_node_id,omitempty"`
	VMID             string `json:"vm_id,omitempty"`
	MaxCaptureBytes  int64  `json:"max_capture_bytes,omitempty"`
}

// sourceSignalRef is signed provenance metadata. The collector does not use it
// to choose commands; it is preserved for deterministic diagnosis-lab binding
// and cross-system audit without inferring business identity from argv text.
type sourceSignalRef struct {
	SupportID string `json:"support_id"`
	SignalID  string `json:"signal_id"`
}

type caseDocument struct {
	CaseID           string            `json:"case_id"`
	SessionID        string            `json:"session_id"`
	SelectedScenario string            `json:"selected_scenario"`
	IncidentWindow   map[string]string `json:"incident_window"`
	IncidentTimezone string            `json:"incident_timezone"`
	Targets          []map[string]any  `json:"targets"`
}

type executionRow struct {
	ItemID          string `json:"item_id"`
	CollectorID     string `json:"collector_id"`
	ExitCode        *int   `json:"exit_code"`
	Status          string `json:"status"`
	StdoutBytes     int64  `json:"stdout_bytes,omitempty"`
	StderrBytes     int64  `json:"stderr_bytes,omitempty"`
	OutputTruncated bool   `json:"output_truncated"`
}

type evidenceFile struct {
	Path         string `json:"path"`
	OriginalName string `json:"original_name"`
	MediaType    string `json:"media_type"`
	Sensitivity  string `json:"sensitivity"`
	SizeBytes    int64  `json:"size_bytes"`
	SHA256       string `json:"sha256"`
}

type evidenceTimeCoverage struct {
	Start string `json:"start"`
	End   string `json:"end"`
}

type evidenceCollectionItem struct {
	CollectorID    string                     `json:"collector_id"`
	Status         string                     `json:"status"`
	Source         string                     `json:"source"`
	SourceTimezone string                     `json:"source_timezone"`
	ClockOffsetMS  int                        `json:"clock_offset_ms"`
	TimeCoverage   *evidenceTimeCoverage      `json:"time_coverage,omitempty"`
	Files          []evidenceFile             `json:"files"`
	ExitCode       *int                       `json:"exit_code"`
	FailureReason  *string                    `json:"failure_reason"`
	VmConsole      *evidenceVmConsoleManifest `json:"vm_console,omitempty"`
}

// evidenceVmConsoleManifest 是 manifest.json collection_items[] 中
// vm_console_capture 条目的附加审计字段（设计文档 §3.4）。
type evidenceVmConsoleManifest struct {
	Executor           string         `json:"executor"`
	OperationVersion   string         `json:"operation_version"`
	CaptureMode        string         `json:"capture_mode"`
	HostNodeID         string         `json:"host_node_id"`
	VMID               string         `json:"vm_id"`
	WakeDecision       string         `json:"wake_decision"`
	WakeResult         string         `json:"wake_result"`
	NearBlack          bool           `json:"near_black"`
	NearBlackAlgorithm string         `json:"near_black_algorithm_revision"`
	QualityMetrics     map[string]any `json:"quality_metrics,omitempty"`
	RecaptureGenerated bool           `json:"recapture_generated"`
}

type evidenceManifest struct {
	SchemaVersion            string                   `json:"schema_version"`
	BundleID                 string                   `json:"bundle_id"`
	CaseID                   string                   `json:"case_id"`
	SessionID                string                   `json:"session_id"`
	BundleType               string                   `json:"bundle_type"`
	ParentBundleID           *string                  `json:"parent_bundle_id"`
	SelectedScenario         string                   `json:"selected_scenario"`
	CollectionProfileVersion string                   `json:"collection_profile_version"`
	CollectionPlanID         string                   `json:"collection_plan_id"`
	CollectorArtifactVersion string                   `json:"collector_artifact_version"`
	CollectorArtifactSHA256  string                   `json:"collector_artifact_sha256"`
	SignatureKeyID           string                   `json:"signature_key_id"`
	GeneratedAt              string                   `json:"generated_at"`
	IncidentWindow           map[string]string        `json:"incident_window"`
	Targets                  []map[string]any         `json:"targets"`
	CollectionItems          []evidenceCollectionItem `json:"collection_items"`
	Encryption               map[string]string        `json:"encryption"`
}

type collectionStats struct {
	Success int
	Failed  int
	Manual  int
}

// vmConsoleCaptureFileInfo 记录一次截图的确定性采集事实（时间、退出码、哈希、大小）。
type vmConsoleCaptureFileInfo struct {
	CapturedAt string `json:"captured_at"`
	ExitCode   int    `json:"exit_code"`
	SHA256     string `json:"sha256"`
	SizeBytes  int64  `json:"size_bytes"`
}

// vmConsoleQualityResult 记录近黑质量检查结果；metrics 字段名与
// backend/shared/vision/near_black.py 的 compute_quality_metrics 完全一致。
type vmConsoleQualityResult struct {
	AlgorithmRevision string         `json:"algorithm_revision"`
	ParseOK           bool           `json:"parse_ok"`
	ParseError        string         `json:"parse_error,omitempty"`
	NearBlack         bool           `json:"near_black"`
	Metrics           map[string]any `json:"metrics"`
}

// vmConsoleCleanupResult 记录宿主机临时文件清理结果；清理失败只记录不阻断。
type vmConsoleCleanupResult struct {
	TempDir   string   `json:"temp_dir"`
	TempFiles []string `json:"temp_files"`
	Removed   bool     `json:"removed"`
	Error     string   `json:"error,omitempty"`
}

// vmConsoleCaptureResult 是 captures/<collection-item-id>/capture-result.json
// 的结构定义：质量统计、唤醒决定与固定操作结果。
type vmConsoleCaptureResult struct {
	Executor         string                    `json:"executor"`
	OperationVersion string                    `json:"operation_version"`
	CaptureMode      string                    `json:"capture_mode"`
	HostNodeID       string                    `json:"host_node_id"`
	VMID             string                    `json:"vm_id"`
	Baseline         vmConsoleCaptureFileInfo  `json:"baseline"`
	Quality          vmConsoleQualityResult    `json:"quality"`
	WakeDecision     string                    `json:"wake_decision"`
	WakeResult       string                    `json:"wake_result"`
	Recapture        *vmConsoleCaptureFileInfo `json:"recapture,omitempty"`
	Cleanup          vmConsoleCleanupResult    `json:"cleanup"`
	SourceSignalRefs []sourceSignalRef         `json:"source_signal_refs"`
}
