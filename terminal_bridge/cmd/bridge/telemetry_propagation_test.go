//go:build telemetry

// U1 集群实现守卫：W3C Random 标志位（0x03）经 logpush 归一化后必须被
// 门面提取为有效远程父上下文，且真实实现开的子 span 继承其 trace。
// 本测试只在 -tags telemetry 下编译运行（CI 双 tag 各跑一遍）；
// 测试函数名自 main_test.go 随迁守恒（原 otel 直调断言改门面断言）。

package main

import (
	"context"
	"testing"

	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/telemetry"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func TestContextFromMessageAcceptsW3CRandomFlag(t *testing.T) {
	// 测试缝：装一个无 exporter 的真实 provider（与生产同构造，默认采样），
	// 否则全局默认 noop provider 不产生 trace，无法验证父子继承。
	previous := otel.GetTracerProvider()
	provider := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(provider)
	defer func() {
		_ = provider.Shutdown(context.Background())
		otel.SetTracerProvider(previous)
	}()

	const traceID = "caa7e3e825ba4a606df189740be1118c"
	const spanID = "cbef2f8fb7e2d3a8"
	ctx := logpush.ContextFromMessage(models.InMessage{
		TraceID:     traceID,
		Traceparent: "00-" + traceID + "-" + spanID + "-03",
	})
	info := telemetry.TraceInfo(ctx)

	if !info.Valid || !info.Remote {
		t.Fatalf("03 trace-flags 应提取为有效远程父上下文: %#v", info)
	}
	if info.TraceID != traceID || info.SpanID != spanID {
		t.Fatalf("父上下文标识不一致: trace_id=%s span_id=%s", info.TraceID, info.SpanID)
	}
	if info.TraceFlags&0x01 == 0 {
		t.Fatal("归一化 03 trace-flags 时必须保留 Sampled 位")
	}

	// 真实实现必须把门面远程父上下文还原回 otel 再开 span：
	// 子 span 继承父 trace（采样决策与直调 otel 时代一致）。
	_, span := telemetry.StartClient(ctx, "terminal_bridge.test.child")
	defer span.End()
	if span.TraceID() != traceID {
		t.Fatalf("子 span 应继承远程父 trace: got=%s want=%s", span.TraceID(), traceID)
	}
}
