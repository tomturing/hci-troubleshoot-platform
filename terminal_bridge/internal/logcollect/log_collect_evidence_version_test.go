package logcollect

import (
	"context"
	"testing"

	"terminal_bridge/internal/shared/evidence"
)

// 注入版本后，每条证据请求都必须带上 X-Bridge-Version。
//
// 为什么这条要单测钉住：旧现场（Q2026112400942）平台侧回答不了"这台跑的哪个版本"，
// 因为启动期的 bridge.startup 只能走直传通道、一失败就永久丢失。版本随业务请求走，
// 才能保证任何一条指标/探测/清单都能反查上报方身份。
func TestEvidenceRequestCarriesBridgeVersion(t *testing.T) {
	t.Cleanup(func() { evidence.SetBridgeVersion("") })
	evidence.SetBridgeVersion("v2.37.0-13-g7ea91e5d")

	cfg := EvidenceConfig{Ticket: "t.1", TraceID: "trace-1"}
	req, err := cfg.newEvidenceRequest(context.Background(), "https://example.test/metrics", []byte("{}"), "")
	if err != nil {
		t.Fatalf("构造请求失败：%v", err)
	}
	if got := req.Header.Get("X-Bridge-Version"); got != "v2.37.0-13-g7ea91e5d" {
		t.Fatalf("X-Bridge-Version=%q，期望注入的版本串", got)
	}
	if got := req.Header.Get("X-Evidence-Ticket"); got != "t.1" {
		t.Fatalf("票据头被破坏：X-Evidence-Ticket=%q", got)
	}
}

// 未注入版本（旧装配、单元测试、集群内无 ldflags）时不能发空头：
// 空头会让平台侧把 "" 当成一次有效上报，反而覆盖已记录的版本。
func TestEvidenceRequestOmitsEmptyBridgeVersion(t *testing.T) {
	t.Cleanup(func() { evidence.SetBridgeVersion("") })
	evidence.SetBridgeVersion("")

	cfg := EvidenceConfig{Ticket: "t.1", TraceID: "trace-1"}
	req, err := cfg.newEvidenceRequest(context.Background(), "https://example.test/metrics", []byte("{}"), "")
	if err != nil {
		t.Fatalf("构造请求失败：%v", err)
	}
	if _, ok := req.Header["X-Bridge-Version"]; ok {
		t.Fatalf("未注入版本时不应出现 X-Bridge-Version 头，实际=%q", req.Header.Get("X-Bridge-Version"))
	}
}
