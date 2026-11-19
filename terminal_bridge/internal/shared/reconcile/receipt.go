package reconcile

import "strings"

// receipt.go —— 统一回执信封（收口设计 §2.5）。
//
// 分层边界：设备标记（remote/marker.go，shell 回显行）与 WS 回执信封是两层。
// acli_sync_result 是 WS 消息不是设备标记；前端解析的是 WS 消息，不直接读设备标记。
// 本包只产出机器可判的 Status/Stage/ErrorCode + 逐步证据，
// LC-* 业务码与中文文案由调用方（main）映射——那是 UI 契约，不进通用骨架。

// Status 是回执终态。
const (
	StatusOK      = "ok"
	StatusError   = "error"
	StatusSkipped = "skipped"
	StatusBlocked = "blocked"
)

// Stage 是失败/结论发生的流水线段。
//
// descriptor 段是落地时新增的一档：inline 超限必须在**发送之前**拒绝
// （补丁 4），此时还没进入 probe，归到 probe 会让"为什么没探测"无从解释。
const (
	StageDescriptor = "descriptor"
	StageProbe      = "probe"
	StageDownload   = "download"
	StageWrite      = "write"
	StageInstall    = "install"
	StageVerify     = "verify"
	StageRollback   = "rollback"
	StagePlugin     = "plugin"
)

// 骨架自有的稳定错误码。调用方把它们映射成自己的 LC-* 业务码。
const (
	CodeDescriptorTooLarge     = "descriptor_too_large"
	CodeDescriptorInvalid      = "descriptor_invalid"
	CodePackageSourceUndef     = "package_source_undefined"
	CodeBusy                   = "busy"
	CodeDowngradeBlocked       = "downgrade_blocked"
	CodeWriteFailed            = "write_failed"
	CodeVerifyFailed           = "verify_failed"
	CodeInstallFailed          = "install_failed"
	CodeInstallUnsupported     = "install_unsupported"
	CodeRollbackRefused        = "rollback_refused"
	CodeProbeFailed            = "probe_failed"
	CodeStateUnavailable       = "state_unavailable"
	CodeRestoreFailed          = "restore_failed"
	CodeNoBackup               = "no_backup"
	CodeInlineDowngradeBlocked = "inline_downgrade_blocked"
)

// Evidence 是一条逐步证据（大小、摘要、耗时、差量三态计数）。
//
// 为什么必须留痕：降级（校验档位、探测失败、inline 改走慢路径）如果不写进
// 证据，"为什么这次走了另一条路"事后无从解释，静默降级就等于伪装成功。
type Evidence struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

// Result 是所有链路统一的回执信封。
type Result struct {
	Status    string     `json:"status"`
	Stage     string     `json:"stage,omitempty"`
	ErrorCode string     `json:"error_code,omitempty"`
	Retryable bool       `json:"retryable,omitempty"`
	Evidence  []Evidence `json:"evidence,omitempty"`
	// Degraded 为真表示结论建立在降级事实上（探测失败后凭账本 Skip、
	// 校验档位低于 sha256、执行器不支持分轨）。前端不得据此显示"已是最新"。
	Degraded bool `json:"degraded,omitempty"`
	// Detail 是机器码之外的可读原因，供调用方拼进自己的文案。
	Detail string `json:"detail,omitempty"`
}

// OK 构造成功回执。
func OK(stage string, evidence ...Evidence) Result {
	return Result{Status: StatusOK, Stage: stage, Evidence: evidence}
}

// Skipped 构造跳过回执。degraded 为真时调用方必须如实上屏"未做判定"。
func Skipped(stage, reason string, degraded bool, evidence ...Evidence) Result {
	return Result{
		Status: StatusSkipped, Stage: stage, Degraded: degraded,
		Detail:   reason,
		Evidence: append(evidence, Evidence{Key: "reason", Value: reason}),
	}
}

// Blocked 构造拒绝回执（并发占用、禁降级）：不是失败终态，调用方应重新入队。
func Blocked(stage, code, reason string, evidence ...Evidence) Result {
	return Result{
		Status: StatusBlocked, Stage: stage, ErrorCode: code, Retryable: true,
		Detail:   reason,
		Evidence: evidence,
	}
}

// Failed 构造失败回执。
func Failed(stage, code, reason string, retryable bool, evidence ...Evidence) Result {
	return Result{
		Status: StatusError, Stage: stage, ErrorCode: code, Retryable: retryable,
		Detail:   reason,
		Evidence: evidence,
	}
}

// WithEvidence 追加证据，返回新值（不修改接收者，便于流水线逐段累积）。
func (r Result) WithEvidence(pairs ...Evidence) Result {
	r.Evidence = append(append([]Evidence(nil), r.Evidence...), pairs...)
	return r
}

// KV 是 Evidence 的简写构造器。
func KV(key, value string) Evidence { return Evidence{Key: key, Value: value} }

// Field 取证据值；不存在返回空串。
func (r Result) Field(key string) string {
	for _, e := range r.Evidence {
		if e.Key == key {
			return e.Value
		}
	}
	return ""
}

// EvidenceMap 把证据摊平成 map，供调用方直接塞进结构化日志。
func (r Result) EvidenceMap() map[string]string {
	out := make(map[string]string, len(r.Evidence))
	for _, e := range r.Evidence {
		out[e.Key] = e.Value
	}
	return out
}

// Summary 把回执压成一行可读摘要（日志/trace 用）。
func (r Result) Summary() string {
	var b strings.Builder
	b.WriteString(r.Status)
	if r.Stage != "" {
		b.WriteString("/" + r.Stage)
	}
	if r.ErrorCode != "" {
		b.WriteString(" code=" + r.ErrorCode)
	}
	if r.Degraded {
		b.WriteString(" degraded=true")
	}
	for _, e := range r.Evidence {
		b.WriteString(" " + e.Key + "=" + e.Value)
	}
	return b.String()
}
