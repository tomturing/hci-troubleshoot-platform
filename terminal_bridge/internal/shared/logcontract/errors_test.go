package logcontract

// 契约测试：Go 侧常量/元数据/展示文案必须与平台 JSON 单一真源完全一致。
// 任何一侧新增而另一侧未同步都会让 CI 失败，避免"码在日志里有、平台不认"。
//
// 迁移说明（阶段 2 A3）：本文件原先住在 internal/logcollect，与业务测试混在一起。
// 错误码既然是跨域契约，它的守卫就应该跟着契约包走，否则 acli/delivery 改动时
// 没有人会想到去跑 logcollect 的测试。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

const errorCatalogRelativePath = "../../../../backend/shared/resolution/catalogs/log_collect_error_catalog.json"

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
func TestErrorCatalogMatchesGoMeta(t *testing.T) {
	catalog := loadErrorCatalog(t)
	// COLLECT：采集期告警（LC-COLLECT-001 等），交付成功也可能存在。
	codePattern := regexp.MustCompile(`^LC-(ENV|INSTALL|EXEC|PLUGIN|SPC|SCRIPT|TRANSFER|PLATFORM|COLLECT)-\d{3}$`)

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
		"invalid_task_id":    ErrPlugInvalidTaskID,
		"spc_dir_missing":    ErrSpcDirMissing,
		"spc_dir_ambiguous":  ErrSpcDirAmbiguous,
		"backup_failed":      ErrSpcBackupFailed,
		"write_rejected":     ErrSpcWriteFailed,
		"host_auth_failed":   ErrSpcSSHAuthFailed,
		"host_timeout":       ErrSpcSSHTimeout,
		"spc_write_failed":   ErrSpcAggregate,
		"spc_module_missing": ErrPlugModuleMissing,
	}
	for errType, want := range cases {
		if got := ValidationErrorCode(errType); got != want {
			t.Fatalf("ValidationErrorCode(%s) = %s, want %s", errType, got, want)
		}
	}
	if got := ValidationErrorCode("unknown"); got != "" {
		t.Fatalf("未知校验类型不应返回码: %s", got)
	}
}

func TestLogCollectErrorFields(t *testing.T) {
	layer, owner, retryable := LogCollectErrorFields(ErrExecPtyRequest)
	if layer != "EXEC" || owner != "customer" || retryable != "false" {
		t.Fatalf("字段补全错误: %s/%s/%s", layer, owner, retryable)
	}
	layer, owner, retryable = LogCollectErrorFields("LC-UNKNOWN-999")
	if layer != "" || owner != "" || retryable != "" {
		t.Fatal("未知码不应补全字段")
	}
}

// TestContractCatalogComplete 防止生成码表再次退化成死代码：
// 71 个运行时码都必须有标题，且文案模板确实被生成。
func TestContractCatalogComplete(t *testing.T) {
	if len(contractErrors) < 71 {
		t.Fatalf("契约运行时码数量不足：%d", len(contractErrors))
	}
	for code, item := range contractErrors {
		if item.Title == "" {
			t.Fatalf("契约码 %s 没有 Title", code)
		}
		if Title(code) != item.Title {
			t.Fatalf("Title(%s) 未返回契约标题", code)
		}
	}
	if len(ContractText) < 5 {
		t.Fatalf("桥侧文案模板未生成：%d 条", len(ContractText))
	}
}
