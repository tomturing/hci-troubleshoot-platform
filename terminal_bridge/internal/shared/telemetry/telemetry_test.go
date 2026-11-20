package telemetry

// 门面本体守卫（无 build tag，桌面/集群两种构建都跑）：
// W3C 传播解析、零值口径、noop span 行为。真实实现的父子继承守卫
// 在 cmd/bridge/telemetry_propagation_test.go（-tags telemetry）。

import (
	"context"
	"testing"
)

const (
	testTraceID = "caa7e3e825ba4a606df189740be1118c"
	testSpanID  = "cbef2f8fb7e2d3a8"
)

func TestExtractValidTraceparent(t *testing.T) {
	ctx := Extract(context.Background(), "00-"+testTraceID+"-"+testSpanID+"-01", "vendor=x")
	info := TraceInfo(ctx)
	if !info.Valid || !info.Remote {
		t.Fatalf("合法 traceparent 应提取为有效远程上下文: %#v", info)
	}
	if info.TraceID != testTraceID || info.SpanID != testSpanID || info.TraceFlags != 0x01 {
		t.Fatalf("标识/flags 不一致: %#v", info)
	}
	if info.Tracestate != "vendor=x" {
		t.Fatalf("tracestate 应随上下文携带: %q", info.Tracestate)
	}
	if got := Traceparent(ctx); got != "00-"+testTraceID+"-"+testSpanID+"-01" {
		t.Fatalf("traceparent 渲染不一致: %s", got)
	}
}

func TestExtractRejectsInvalidTraceparent(t *testing.T) {
	invalid := []string{
		"",
		"00-" + testTraceID + "-" + testSpanID, // 缺 flags
		"01-" + testTraceID + "-" + testSpanID + "-01",              // 未来版本（与现役解析器同口径拒绝）
		"00-00000000000000000000000000000000-" + testSpanID + "-01", // 全零 trace-id
		"00-" + testTraceID + "-0000000000000000-01",                // 全零 span-id
		"00-" + testTraceID + "-" + testSpanID + "-zz",              // 非法 hex flags
		"00-short-" + testSpanID + "-01",                            // 长度不足
	}
	for _, tp := range invalid {
		ctx := Extract(context.Background(), tp, "")
		if info := TraceInfo(ctx); info.Valid {
			t.Fatalf("非法 traceparent 不应产生有效上下文: %q -> %#v", tp, info)
		}
		if got := Traceparent(ctx); got != "" {
			t.Fatalf("无效上下文不应渲染 traceparent: %q -> %s", tp, got)
		}
	}
}

func TestTraceInfoZeroValueMatchesOTelShape(t *testing.T) {
	// 无效上下文必须返回全零 ID（对齐 OTel SpanContext 零值）：
	// 桌面剥离 otel 前后，logpush 日志字段形状不能变。
	info := TraceInfo(context.Background())
	if info.Valid || info.TraceID != zeroTraceID || info.SpanID != zeroSpanID || info.TraceFlags != 0 {
		t.Fatalf("零值口径不一致: %#v", info)
	}
}
