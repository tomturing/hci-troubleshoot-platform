package main

// evidence_wire.go 是证据门面的装配接线：只有装配层认识 shared/obslog，
// 业务域只依赖 shared/evidence 的 Sink 接口（阶段 2 验收：域内 import obslog 为 0）。
//
// 这里不做任何加工，纯粹转发——门面不引入第二种日志语义，避免出现
// "域里看到的级别/字段"与"落盘内容"不一致。

import (
	"context"

	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
)

// obslogSink 把门面接到 obslog 的真实实现上。
type obslogSink struct{}

func (obslogSink) Emit(level, event, msg, traceID, caseID, nodeIP, customUI string, extra map[string]any) {
	obslog.Emit(level, event, msg, traceID, caseID, nodeIP, customUI, extra)
}

func (obslogSink) EmitContext(ctx context.Context, level, event, message string, req models.ExecRequestContext, extra map[string]any) {
	obslog.EmitContext(ctx, level, event, message, req, extra)
}

func (obslogSink) RedactCommand(command string) string { return obslog.RedactCommand(command) }

func (obslogSink) RedactSensitiveText(value string) string { return obslog.RedactSensitiveText(value) }

func (obslogSink) TraceparentFromContext(ctx context.Context) string {
	return obslog.TraceparentFromContext(ctx)
}

func (obslogSink) ContextFromMessage(msg models.InMessage) context.Context {
	return obslog.ContextFromMessage(msg)
}

func (obslogSink) DeterministicArtifactID(execID string) string {
	return obslog.DeterministicArtifactID(execID)
}

// LogDir 返回证据目录：交付作业的本地副本与 job 文件必须同源，
// 否则现场打包取证时会漏掉一半（历史事故：只带走日志、没带走 job 快照）。
func (obslogSink) LogDir() string { return obslog.DefaultHub.LogDir }

// installEvidenceSink 必须在 obslog 初始化之后、任何业务 goroutine 启动之前调用。
//
// version 由装配层从 ldflags 注入串取（app.GetVersion），业务域不认构建元数据：
// 采集侧只负责把它塞进 X-Bridge-Version，平台据此记录"数据是哪个 bridge 发的"。
func installEvidenceSink(version string) {
	evidence.Set(obslogSink{})
	evidence.SetBridgeVersion(version)
	// 传输证据端口由采集域实现：交付域只表达"为这个 task_id 报进度/报包清单"，
	// 票据与 TLS 放宽留在采集侧（阶段 2 A3 收尾，消除 delivery -> logcollect）。
	evidence.SetTransfer(logcollect.NewTransferReporter())
}
