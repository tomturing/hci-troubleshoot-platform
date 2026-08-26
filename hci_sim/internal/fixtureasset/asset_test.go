package fixtureasset

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRenderUsesFrozenBindingsAndCurrentKeyword(t *testing.T) {
	template := Asset{Content: json.RawMessage(`{"stdout_template":"任务={{KEYWORD}}，目标={{TARGET}}"}`)}
	instance := Asset{Content: json.RawMessage(`{"bindings":{"KEYWORD":"旧关键字","TARGET":"Server-IMG"}}`)}
	stdout, err := Render(template, instance, "启动虚拟机")
	if err != nil {
		t.Fatalf("Render() error = %v", err)
	}
	if stdout != "任务=启动虚拟机，目标=Server-IMG" {
		t.Fatalf("stdout = %q", stdout)
	}
}

func TestRenderRejectsUnboundVariable(t *testing.T) {
	_, err := Render(Asset{Content: json.RawMessage(`{"stdout_template":"{{MISSING}}"}`)}, Asset{Content: json.RawMessage(`{"bindings":{}}`)}, "")
	if err == nil || !strings.Contains(err.Error(), "未绑定") {
		t.Fatalf("expected unbound variable error, got %v", err)
	}
}

func TestValidateCreateRequiresTemplateReferenceForInstance(t *testing.T) {
	err := ValidateCreate(CreateRequest{AssetKey: "qkv_task.example", AssetType: TypeInstance, SignalType: "qkv_task", Content: json.RawMessage(`{}`), CategoryBaseline: json.RawMessage(`{}`), CatalogBaseline: json.RawMessage(`{}`)})
	if err == nil {
		t.Fatal("expected instance template reference validation error")
	}
}
