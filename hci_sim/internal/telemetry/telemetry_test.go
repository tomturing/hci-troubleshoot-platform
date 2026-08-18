package telemetry

import (
	"context"
	"testing"
	"time"
)

func TestInitAcceptsCurrentResourceSchema(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
	t.Setenv("HCI_SIM_ENVIRONMENT", "test")

	shutdown, err := Init(context.Background())
	if err != nil {
		t.Fatalf("初始化当前 OTel schema 失败: %v", err)
	}
	if shutdown == nil {
		t.Fatal("初始化成功但未返回 shutdown 函数")
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		t.Fatalf("关闭 OTel provider 失败: %v", err)
	}
}
