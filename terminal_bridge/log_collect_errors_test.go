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
	codePattern := regexp.MustCompile(`^LC-(ENV|INSTALL|EXEC|PLUGIN|SPC|TRANSFER|PLATFORM)-\d{3}$`)

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

// TestLogCollectFailureLineSkipsWrapper 守住 2026-11-12 的关键回归：
// acli 真实原因在 FATA 包装行之前，不能再被跳过。
func TestLogCollectFailureLineSkipsWrapper(t *testing.T) {
	output := `2026/11/12 18:26:57 {"@timestamp":"2026-11-12T18:26:57","level":"error","content":"[0] internal/cpu_linux.go:29 open /sys/fs/cgroup/cpu/cpuset.cpus: no such file or directory"}
收集专项core不存在！
FATA[0000] execing command in container: command terminated with exit code 1
`
	if got := logCollectFailureLine(output); got != "收集专项core不存在！" {
		t.Fatalf("应取业务行，得到 %q", got)
	}
	if !logCollectModuleMissing(output) {
		t.Fatal("应识别出专项不存在")
	}
}

// TestLogCollectFailureLineKeepsTimestampedReason 原因行带时间戳前缀时，
// 只能剥前缀、不能整行丢弃（否则真实原因消失，界面又退回"日志收集失败"）。
func TestLogCollectFailureLineKeepsTimestampedReason(t *testing.T) {
	output := "2026/11/12 18:26:57 收集专项core不存在！\n"
	if got := logCollectFailureLine(output); got != "收集专项core不存在！" {
		t.Fatalf("应剥掉时间戳保留原因，得到 %q", got)
	}
	if !logCollectModuleMissing(output) {
		t.Fatal("带时间戳时也要识别出专项不存在")
	}
}

// TestLogCollectFailureLineEnglish 英文 locale 的原因行同样要能取到。
func TestLogCollectFailureLineEnglish(t *testing.T) {
	output := "collector started\nError: scenario core not found\n"
	if got := logCollectFailureLine(output); got != "Error: scenario core not found" {
		t.Fatalf("应取英文原因行，得到 %q", got)
	}
}

// TestLogCollectFailureLineFallback FATA 只能兜底，不能作为唯一来源。
func TestLogCollectFailureLineFallback(t *testing.T) {
	if got := logCollectFailureLine("FATA[0000] execing command in container: exit 1\n"); got == "" {
		t.Fatal("没有业务行时应保留 FATA 兜底")
	}
	if got := logCollectFailureLine(""); got != "" {
		t.Fatalf("空输出应返回空，得到 %q", got)
	}
}

// TestSpcErrorTitle 标题必须是可处置的中文原因，不是"节点不可达"。
func TestSpcErrorTitle(t *testing.T) {
	cases := map[string]string{
		"host_auth_failed": "节点 SSH 认证失败",
		"host_timeout":     "节点命令执行超时",
		"spc_dir_missing":  "场景清单目录缺失",
		"spc_file_missing": "场景清单文件缺失",
	}
	for errType, want := range cases {
		if got := spcErrorTitle(errType); got != want {
			t.Fatalf("spcErrorTitle(%s) = %q，期望 %q", errType, got, want)
		}
	}
}
