package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

const errorCatalogRelativePath = "../backend/shared/resolution/catalogs/log_collect_error_catalog.json"

type catalogFile struct {
	Errors []struct {
		Code      string `json:"code"`
		Layer     string `json:"layer"`
		Stage     string `json:"stage"`
		Owner     string `json:"owner"`
		Retryable any    `json:"retryable"`
	} `json:"errors"`
}

func loadErrorCatalog(t *testing.T) catalogFile {
	t.Helper()
	path := filepath.Clean(errorCatalogRelativePath)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取错误码目录失败: %v", err)
	}
	var parsed catalogFile
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("解析错误码目录失败: %v", err)
	}
	if len(parsed.Errors) == 0 {
		t.Fatal("错误码目录为空")
	}
	return parsed
}

func normalizeRetryable(value any) string {
	switch typed := value.(type) {
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case string:
		return typed
	default:
		return ""
	}
}

// TestErrorCatalogMatchesGoMeta 强制 Go 常量表与 JSON 单一真源完全一致。
// 任何一侧新增/修改而未同步，CI 直接失败，避免"码在日志里有、平台不认"。
func TestErrorCatalogMatchesGoMeta(t *testing.T) {
	catalog := loadErrorCatalog(t)
	codePattern := regexp.MustCompile(`^LC-(ENV|INSTALL|EXEC|PLUGIN|TRANSFER|PLATFORM)-\d{3}$`)

	seen := make(map[string]bool, len(catalog.Errors))
	for _, item := range catalog.Errors {
		if !codePattern.MatchString(item.Code) {
			t.Fatalf("错误码格式不合法: %s", item.Code)
		}
		if seen[item.Code] {
			t.Fatalf("错误码重复: %s", item.Code)
		}
		seen[item.Code] = true

		meta, ok := logCollectErrorMeta[item.Code]
		if !ok {
			t.Fatalf("Go 侧缺少错误码: %s", item.Code)
		}
		if meta.Layer != item.Layer {
			t.Fatalf("%s layer 不一致: go=%s json=%s", item.Code, meta.Layer, item.Layer)
		}
		if meta.Owner != item.Owner {
			t.Fatalf("%s owner 不一致: go=%s json=%s", item.Code, meta.Owner, item.Owner)
		}
		if meta.Stage != item.Stage {
			t.Fatalf("%s stage 不一致: go=%s json=%s", item.Code, meta.Stage, item.Stage)
		}
		if got, want := meta.Retryable, normalizeRetryable(item.Retryable); got != want {
			t.Fatalf("%s retryable 不一致: go=%s json=%s", item.Code, got, want)
		}
	}

	for code := range logCollectErrorMeta {
		if !seen[code] {
			t.Fatalf("Go 侧存在目录未定义的错误码: %s", code)
		}
	}
}

func TestValidationErrorCodeMapping(t *testing.T) {
	cases := map[string]string{
		"invalid_ticket":     ErrPlugInvalidTicket,
		"invalid_module":     ErrPlugInvalidModule,
		"invalid_date":       ErrPlugInvalidDate,
		"invalid_ips":        ErrPlugInvalidIPs,
		"invalid_path":       ErrPlugInvalidPath,
		"invalid_remote_url": ErrPlugInvalidURL,
		"invalid_action":     ErrPlugInvalidAction,
	}
	for errType, want := range cases {
		if got := validationErrorCode(errType); got != want {
			t.Fatalf("validationErrorCode(%s) = %s, want %s", errType, got, want)
		}
	}
	if got := validationErrorCode("unknown"); got != "" {
		t.Fatalf("未知校验类型不应返回码: %s", got)
	}
}

func TestLogCollectErrorFields(t *testing.T) {
	layer, owner, retryable := logCollectErrorFields(ErrExecPtyRequest)
	if layer != "EXEC" || owner != "customer" || retryable != "false" {
		t.Fatalf("字段补全错误: %s/%s/%s", layer, owner, retryable)
	}
	layer, owner, retryable = logCollectErrorFields("LC-UNKNOWN-999")
	if layer != "" || owner != "" || retryable != "" {
		t.Fatal("未知码不应补全字段")
	}
}
