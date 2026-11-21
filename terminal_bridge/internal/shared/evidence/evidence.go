// Package evidence 是日志证据通道的**门面**：业务域只依赖这里，
// 真实实现（shared/obslog）由装配层在启动时注入。
//
// 为什么需要门面：obslog 是横切关注点，改造前被 7 个包直接 import（含 sshx /
// terminal / 4 个业务域），签一变更就波及全仓，域单测也不得不构造 logHub。
// 这与 shared/telemetry 已经走通的"门面 + 双实现"范式不一致——同一个仓库里
// 两条链路执行不同的依赖纪律。阶段 2 把 obslog 也收敛到门面之后，域内
// `import shared/obslog` 为 0，仅装配层认识具体实现。
//
// 门面只做转发，不持有状态；未注入时退化为 Nop，保证测试与命令行工具可直接跑。
package evidence

import (
	"context"
	"sync/atomic"

	"terminal_bridge/internal/shared/models"
)

// Sink 是证据通道的全部能力。扩展它等于扩展跨域契约，必须同步改装配层的适配器。
type Sink interface {
	// Emit 写一条证据事件。
	Emit(level, event, msg, traceID, caseID, nodeIP, customUI string, extra map[string]any)
	// EmitContext 在已有链路上下文里写事件（携带 traceparent）。
	EmitContext(ctx context.Context, level, event, message string, req models.ExecRequestContext, extra map[string]any)
	// RedactCommand / RedactSensitiveText 是落盘前的脱敏原语。
	RedactCommand(command string) string
	RedactSensitiveText(value string) string
	// TraceparentFromContext / ContextFromMessage 是链路传播原语。
	TraceparentFromContext(ctx context.Context) string
	ContextFromMessage(msg models.InMessage) context.Context
	// DeterministicArtifactID 由 execID 派生稳定制品 ID。
	DeterministicArtifactID(execID string) string
	// LogDir 返回证据目录（交付作业的本地副本与 job 文件同源）。
	LogDir() string
}

type nopSink struct{}

func (nopSink) Emit(string, string, string, string, string, string, string, map[string]any) {}
func (nopSink) EmitContext(context.Context, string, string, string, models.ExecRequestContext, map[string]any) {
}
func (nopSink) RedactCommand(command string) string           { return command }
func (nopSink) RedactSensitiveText(value string) string       { return value }
func (nopSink) TraceparentFromContext(context.Context) string { return "" }
func (nopSink) ContextFromMessage(models.InMessage) context.Context {
	return context.Background()
}
func (nopSink) DeterministicArtifactID(execID string) string { return execID }
func (nopSink) LogDir() string                               { return "" }

// sink 用 atomic.Value 保存：启动时注入一次，之后全仓只读，
// 避免在 goroutine 里读到半初始化的接口值（race）。
var sink atomic.Value

func load() Sink {
	if v := sink.Load(); v != nil {
		return v.(Sink)
	}
	return nopSink{}
}

// Set 由装配层在启动时调用一次；传 nil 视为不注入（保持 Nop）。
func Set(s Sink) {
	if s == nil {
		return
	}
	sink.Store(s)
}

// Emit 写一条证据事件（门面入口，签名与历史 obslog.Emit 完全一致）。
func Emit(level, event, msg, traceID, caseID, nodeIP, customUI string, extra map[string]any) {
	load().Emit(level, event, msg, traceID, caseID, nodeIP, customUI, extra)
}

// EmitContext 在已有链路上下文里写事件。
func EmitContext(ctx context.Context, level, event, message string, req models.ExecRequestContext, extra map[string]any) {
	load().EmitContext(ctx, level, event, message, req, extra)
}

// RedactCommand 脱敏一条将要落盘/上报的命令。
func RedactCommand(command string) string { return load().RedactCommand(command) }

// RedactSensitiveText 脱敏自由文本中的凭据。
func RedactSensitiveText(value string) string { return load().RedactSensitiveText(value) }

// TraceparentFromContext 取当前链路的 traceparent。
func TraceparentFromContext(ctx context.Context) string { return load().TraceparentFromContext(ctx) }

// ContextFromMessage 从入站消息还原链路上下文。
func ContextFromMessage(msg models.InMessage) context.Context { return load().ContextFromMessage(msg) }

// DeterministicArtifactID 由 execID 派生稳定制品 ID。
func DeterministicArtifactID(execID string) string { return load().DeterministicArtifactID(execID) }

// LogDir 返回证据目录。
func LogDir() string { return load().LogDir() }
