//go:build !telemetry

// noop 实现守卫（桌面默认构建）：Start 不改写父上下文、span 全操作空转、
// 零值口径与 Init 空 shutdown。真实实现的对应守卫在 otel 侧 tagged 测试。

package telemetry

import (
	"context"
	"errors"
	"testing"
)

func TestNoopSpanPreservesParentContext(t *testing.T) {
	ctx := Extract(context.Background(), "00-"+testTraceID+"-"+testSpanID+"-01", "")
	// 默认构建（!telemetry）下 Start 必须原样携带远程父上下文：
	// 与现状 noop tracer「子 span 不改写 ctx 标识」一致。
	childCtx, span := Start(ctx, "test.noop")
	defer span.End()
	if got := Traceparent(childCtx); got != "00-"+testTraceID+"-"+testSpanID+"-01" {
		t.Fatalf("noop Start 不应改写父上下文标识: %s", got)
	}
	// noop span 的全部操作零成本空转，TraceID 为全零（桌面现状口径）。
	span.AddEvent("evt", String("k", "v"), Bool("b", true), Int("i", 1), Int64("l", 2))
	span.SetStatus(StatusError, "msg")
	span.RecordError(errors.New("x"))
	span.SetAttributes(String("k2", "v2"))
	if span.TraceID() != zeroTraceID {
		t.Fatalf("noop span TraceID 应为全零: %s", span.TraceID())
	}
	if _, ok := SpanFromContext(context.Background()).(noopSpan); !ok {
		t.Fatal("无 span 上下文应返回 noop span")
	}
}

func TestInitNoopShutdown(t *testing.T) {
	shutdown, err := Init(context.Background(), "", "v-test", "inst", "local")
	if err != nil {
		t.Fatalf("Init 不应失败: %v", err)
	}
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown 不应失败: %v", err)
	}
}
