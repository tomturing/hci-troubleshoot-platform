// Package telemetry 是可观测性门面（架构重构设计 §2.6 / §6.2，U1）。
//
// 调用方只依赖本包的最小 API（Start/StartClient/AddEvent/SetStatus/End 等），
// 签名零 otel 类型（硬禁令 5）：真实实现（build tag telemetry，集群镜像）包装
// OTel SDK；默认实现为零成本 noop（桌面 exe，OTel 从未在桌面启用过）。
//
// 传播（W3C traceparent）在本包内用纯标准库解析，不依赖 otel propagator——
// obslog 是证据通道、不可随 build tag 剥离（设计 §2.6 裁决），桌面剥离 otel
// 后日志仍须携带平台下发的 trace 关联。两种实现的日志字段形状完全一致：
// 无效上下文一律返回全零 ID（对齐 OTel SpanContext 零值语义）。
package telemetry

import (
	"context"
	"encoding/hex"
	"fmt"
	"strings"
)

// 状态码常量（门面自有口径，真实实现映射到 otel/codes）。
const (
	StatusUnset = "unset"
	StatusOK    = "ok"
	StatusError = "error"
)

// OTel SpanContext 零值的字符串形态：无效上下文的 TraceID/SpanID 取它，
// 保证 noop 与真实实现下日志/回执字段形状一致（桌面现状即全零）。
const (
	zeroTraceID = "00000000000000000000000000000000"
	zeroSpanID  = "0000000000000000"
)

// Attr 是门面自有的属性类型（string/bool/int64 三档够用，禁止泄漏 otel 类型）。
type Attr struct {
	key  string
	kind attrKind
	str  string
	num  int64
	boo  bool
}

type attrKind uint8

const (
	kindString attrKind = iota
	kindBool
	kindInt64
)

func String(key, value string) Attr { return Attr{key: key, kind: kindString, str: value} }
func Bool(key string, value bool) Attr {
	return Attr{key: key, kind: kindBool, boo: value}
}
func Int(key string, value int) Attr { return Int64(key, int64(value)) }
func Int64(key string, value int64) Attr {
	return Attr{key: key, kind: kindInt64, num: value}
}

// Span 是一次跟踪片段的最小操作面。noop 实现全部空转、零分配。
type Span interface {
	AddEvent(name string, attrs ...Attr)
	SetStatus(code, msg string)
	RecordError(err error)
	SetAttributes(attrs ...Attr)
	// TraceID 返回本 span 的 trace 标识；noop 为全零（对齐桌面现状）。
	TraceID() string
	End()
}

// SpanInfo 是 ctx 携带的跟踪标识（门面自有类型）。
// Valid=false 时 TraceID/SpanID 为全零、TraceFlags=0，与 OTel 零值一致。
type SpanInfo struct {
	TraceID    string
	SpanID     string
	TraceFlags byte
	Tracestate string
	Valid      bool
	Remote     bool
}

type ctxKey struct{}

type spanKind uint8

const (
	kindInternal spanKind = iota
	kindClient
)

// implementation 是双实现缝：noop.go（!telemetry）与 otel.go（telemetry）
// 各自在 init() 里认领；门面 API 永远只走这条缝。
type implementation interface {
	start(ctx context.Context, name string, kind spanKind, attrs []Attr) (context.Context, Span)
	spanFromContext(ctx context.Context) Span
	init(ctx context.Context, endpoint, serviceVersion, instanceID, environment string) (func(context.Context) error, error)
}

var impl implementation

// Start 开一个 internal span；StartClient 开 client span（对 HCI 的出站调用）。
func Start(ctx context.Context, name string, attrs ...Attr) (context.Context, Span) {
	return impl.start(ctx, name, kindInternal, attrs)
}

func StartClient(ctx context.Context, name string, attrs ...Attr) (context.Context, Span) {
	return impl.start(ctx, name, kindClient, attrs)
}

// SpanFromContext 取 ctx 上的当前 span；没有则返回 noop span（与 OTel 口径一致）。
func SpanFromContext(ctx context.Context) Span { return impl.spanFromContext(ctx) }

// TraceInfo 读 ctx 携带的跟踪标识；无上下文时返回全零无效值。
func TraceInfo(ctx context.Context) SpanInfo {
	if info, ok := ctx.Value(ctxKey{}).(SpanInfo); ok {
		return info
	}
	return SpanInfo{TraceID: zeroTraceID, SpanID: zeroSpanID}
}

// Traceparent 渲染 W3C traceparent 头；无效上下文返回空串（不渲染全零头）。
func Traceparent(ctx context.Context) string {
	info := TraceInfo(ctx)
	if !info.Valid {
		return ""
	}
	return fmt.Sprintf("00-%s-%s-%02x", info.TraceID, info.SpanID, info.TraceFlags)
}

// Extract 从 W3C traceparent/tracestate 还原远程父上下文（纯标准库解析）。
// traceparent 非法时原样返回 ctx。调用方负责旧版 Go SDK 的 flags 归一化
// （obslog.NormalizeTraceparentForLegacyGo），本函数按规范解析 flags 字节。
func Extract(ctx context.Context, traceparent, tracestate string) context.Context {
	info := parseTraceparent(traceparent)
	if !info.Valid {
		return ctx
	}
	info.Remote = true
	info.Tracestate = tracestate
	return context.WithValue(ctx, ctxKey{}, info)
}

// parseTraceparent 只接受 version 00（与现役 OTel v1.24 解析器同口径）：
// 32 位 hex trace-id、16 位 hex span-id、2 位 hex flags，全零 ID 非法。
func parseTraceparent(traceparent string) SpanInfo {
	parts := strings.Split(traceparent, "-")
	if len(parts) != 4 || parts[0] != "00" {
		return SpanInfo{}
	}
	if len(parts[1]) != 32 || len(parts[2]) != 16 || len(parts[3]) != 2 {
		return SpanInfo{}
	}
	traceID, err := hex.DecodeString(parts[1])
	if err != nil || isAllZero(traceID) {
		return SpanInfo{}
	}
	spanID, err := hex.DecodeString(parts[2])
	if err != nil || isAllZero(spanID) {
		return SpanInfo{}
	}
	flags, err := hex.DecodeString(parts[3])
	if err != nil {
		return SpanInfo{}
	}
	return SpanInfo{TraceID: parts[1], SpanID: parts[2], TraceFlags: flags[0], Valid: true}
}

func isAllZero(b []byte) bool {
	for _, v := range b {
		if v != 0 {
			return false
		}
	}
	return true
}

// Init 装配跟踪导出：endpoint 为空则不启用导出（桌面现状）；
// 返回 shutdown 函数。真实实现建 OTLP HTTP exporter + TracerProvider，
// noop 实现直接返回空 shutdown。serviceVersion/instanceID/environment
// 由装配层注入（门面不认识任何业务域）。
func Init(ctx context.Context, endpoint, serviceVersion, instanceID, environment string) (func(context.Context) error, error) {
	return impl.init(ctx, strings.TrimSpace(endpoint), serviceVersion, instanceID, environment)
}

// noopSpan 是零成本空 span：两种实现都会用到（noop 全程、真实实现的
// SpanFromContext 兜底），故放在无 build tag 的门面文件里。
type noopSpan struct{}

func (noopSpan) AddEvent(string, ...Attr) {}
func (noopSpan) SetStatus(string, string) {}
func (noopSpan) RecordError(error)        {}
func (noopSpan) SetAttributes(...Attr)    {}
func (noopSpan) TraceID() string          { return zeroTraceID }
func (noopSpan) End()                     {}
