//go:build !telemetry

// 桌面默认实现：零成本 noop（架构重构设计 §6.2）。
// 桌面 exe 从未启用 OTel 导出（HCI_BRIDGE_OTEL_ENDPOINT 只在集群 Helm 注入），
// 本实现让桌面产物编译期就不链接 OTel/gRPC/protobuf（U1 验收：nm 零 otel 符号）。
// 传播解析在门面本体（纯标准库），日志的 trace 关联不受剥离影响。

package telemetry

import "context"

func init() { impl = noopImpl{} }

type noopImpl struct{}

func (noopImpl) start(ctx context.Context, _ string, _ spanKind, _ []Attr) (context.Context, Span) {
	// ctx 原样返回：远程父上下文（平台 traceparent）继续携带，
	// 与现状 noop tracer 下「子 span 不改写 ctx 标识」的行为一致。
	return ctx, noopSpan{}
}

func (noopImpl) spanFromContext(context.Context) Span { return noopSpan{} }

func (noopImpl) init(context.Context, string, string, string, string) (func(context.Context) error, error) {
	return func(context.Context) error { return nil }, nil
}
