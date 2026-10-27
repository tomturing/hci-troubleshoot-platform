package main

// 日志收集全链路错误码（Go 侧）。
//
// 单一真源：backend/shared/resolution/catalogs/log_collect_error_catalog.json。
// 本文件的常量与元数据必须与 JSON 完全一致，由 log_collect_errors_test.go 强校验；
// 任何一侧新增而另一侧未同步都会导致 CI 失败，避免"码在日志里有、平台不认"。
//
// 定界原则：
//   1. 谁失败谁出码，禁止跨层改写；
//   2. 一次失败只有一个主码，其余进 causes；
//   3. 不得静默降级（PTY/解析失败必须出码）。

import (
	"context"
	"errors"
)

const (
	ErrEnvArchUnsupported = "LC-ENV-001"
	ErrEnvArchProbeFailed = "LC-ENV-002"
	ErrEnvAcliMissing     = "LC-ENV-003"
	ErrEnvDiskLow         = "LC-ENV-004"
	ErrEnvLogServerDown   = "LC-ENV-005"
	ErrEnvSessionMissing  = "LC-ENV-006"
	ErrEnvSSHAuthFailed   = "LC-ENV-007"
	ErrEnvNodeOffline     = "LC-ENV-008"

	ErrInstallReqFailed = "LC-INSTALL-001"
	ErrInstallDownload  = "LC-INSTALL-002"
	ErrInstallBadStatus = "LC-INSTALL-003"
	ErrInstallUpload    = "LC-INSTALL-004"
	ErrInstallExec      = "LC-INSTALL-005"
	ErrInstallVerify    = "LC-INSTALL-006"

	ErrExecPtyRequest = "LC-EXEC-001"
	ErrExecTimeout    = "LC-EXEC-002"
	ErrExecNonZero    = "LC-EXEC-003"
	ErrExecStart      = "LC-EXEC-004"
	ErrExecWait       = "LC-EXEC-005"

	ErrPlugInvalidTicket = "LC-PLUGIN-001"
	ErrPlugInvalidModule = "LC-PLUGIN-002"
	ErrPlugInvalidDate   = "LC-PLUGIN-003"
	ErrPlugInvalidIPs    = "LC-PLUGIN-004"
	ErrPlugInvalidPath   = "LC-PLUGIN-005"
	ErrPlugInvalidURL    = "LC-PLUGIN-006"
	ErrPlugTaskConflict  = "LC-PLUGIN-007"
	ErrPlugTaskNotFound  = "LC-PLUGIN-008"
	ErrPlugProgressDegr  = "LC-PLUGIN-009"
	ErrPlugCollectFailed = "LC-PLUGIN-010"
	ErrPlugCancelFailed  = "LC-PLUGIN-011"
	ErrPlugPartialHosts  = "LC-PLUGIN-012"
	ErrPlugInvalidAction = "LC-PLUGIN-013"

	ErrTransferArchiveMissing = "LC-TRANSFER-001"
	ErrTransferChecksum       = "LC-TRANSFER-002"
	ErrTransferChunkFailed    = "LC-TRANSFER-003"
	ErrTransferResumeFailed   = "LC-TRANSFER-004"
	ErrTransferTooLarge       = "LC-TRANSFER-005"

	ErrPlatUnauthorized = "LC-PLATFORM-001"
	ErrPlatForbidden    = "LC-PLATFORM-002"
	ErrPlatTransition   = "LC-PLATFORM-003"
	ErrPlatRegression   = "LC-PLATFORM-004"
	ErrPlatDuplicate    = "LC-PLATFORM-005"
	ErrPlatDBDown       = "LC-PLATFORM-006"
	// 日志包上传的平台侧存储故障：目录不可写 / 空间不足。
	// 由 case-service 产生（不是桥侧），但同属 log_collect 错误码体系，
	// 前端按同一个 problem 结构展示，避免出现裸 500。
	ErrPlatStorageUnwritable = "LC-PLATFORM-007"
	ErrPlatStorageLow        = "LC-PLATFORM-008"
	// 交付并发闸门：同一任务已有交付作业在跑时拒绝重复签发。
	// 由 case-service 的 POST /tasks/{id}/deliver 产生。
	ErrPlatDeliveryInFlight = "LC-PLATFORM-009"
)

// errorMeta 与 JSON 目录字段一一对应（retryable 统一为字符串：true/false/manual）。
type errorMeta struct {
	Layer     string
	Owner     string
	Retryable string
	Stage     string
}

var logCollectErrorMeta = map[string]errorMeta{
	ErrPlatDeliveryInFlight: {"PLATFORM", "customer", "true", "api"},

	ErrEnvArchUnsupported: {"ENV", "customer", "false", "probe"},
	ErrEnvArchProbeFailed: {"ENV", "customer", "true", "probe"},
	ErrEnvAcliMissing:     {"ENV", "customer", "true", "probe"},
	ErrEnvDiskLow:         {"ENV", "customer", "manual", "probe"},
	ErrEnvLogServerDown:   {"ENV", "customer", "manual", "probe"},
	ErrEnvSessionMissing:  {"ENV", "platform", "true", "exec"},
	ErrEnvSSHAuthFailed:   {"ENV", "customer", "manual", "exec"},
	ErrEnvNodeOffline:     {"ENV", "customer", "manual", "collect"},

	ErrInstallReqFailed: {"INSTALL", "platform", "true", "install"},
	ErrInstallDownload:  {"INSTALL", "customer", "true", "install"},
	ErrInstallBadStatus: {"INSTALL", "platform", "true", "install"},
	ErrInstallUpload:    {"INSTALL", "customer", "true", "install"},
	ErrInstallExec:      {"INSTALL", "customer", "manual", "install"},
	ErrInstallVerify:    {"INSTALL", "product", "manual", "install"},

	ErrExecPtyRequest: {"EXEC", "customer", "false", "exec"},
	ErrExecTimeout:    {"EXEC", "customer", "true", "exec"},
	ErrExecNonZero:    {"EXEC", "product", "false", "exec"},
	ErrExecStart:      {"EXEC", "customer", "true", "exec"},
	ErrExecWait:       {"EXEC", "customer", "true", "exec"},

	ErrPlugInvalidTicket: {"PLUGIN", "platform", "false", "create"},
	ErrPlugInvalidModule: {"PLUGIN", "platform", "false", "create"},
	ErrPlugInvalidDate:   {"PLUGIN", "platform", "false", "create"},
	ErrPlugInvalidIPs:    {"PLUGIN", "platform", "false", "create"},
	ErrPlugInvalidPath:   {"PLUGIN", "platform", "false", "create"},
	ErrPlugInvalidURL:    {"PLUGIN", "platform", "false", "create"},
	ErrPlugTaskConflict:  {"PLUGIN", "platform", "manual", "create"},
	ErrPlugTaskNotFound:  {"PLUGIN", "platform", "false", "progress"},
	ErrPlugProgressDegr:  {"PLUGIN", "product", "false", "progress"},
	ErrPlugCollectFailed: {"PLUGIN", "customer", "manual", "collect"},
	ErrPlugCancelFailed:  {"PLUGIN", "customer", "true", "cancel"},
	ErrPlugPartialHosts:  {"PLUGIN", "customer", "manual", "collect"},
	ErrPlugInvalidAction: {"PLUGIN", "platform", "false", "create"},

	ErrTransferArchiveMissing: {"TRANSFER", "customer", "manual", "package"},
	ErrTransferChecksum:       {"TRANSFER", "platform", "true", "upload"},
	ErrTransferChunkFailed:    {"TRANSFER", "customer", "true", "upload"},
	ErrTransferResumeFailed:   {"TRANSFER", "platform", "manual", "upload"},
	ErrTransferTooLarge:       {"TRANSFER", "platform", "false", "upload"},

	ErrPlatUnauthorized: {"PLATFORM", "platform", "true", "api"},
	ErrPlatForbidden:    {"PLATFORM", "platform", "false", "api"},
	ErrPlatTransition:   {"PLATFORM", "platform", "false", "api"},
	ErrPlatRegression:   {"PLATFORM", "platform", "false", "api"},
	ErrPlatDuplicate:    {"PLATFORM", "platform", "manual", "api"},
	ErrPlatDBDown:       {"PLATFORM", "platform", "true", "api"},

	ErrPlatStorageUnwritable: {"PLATFORM", "platform", "manual", "upload"},
	ErrPlatStorageLow:        {"PLATFORM", "platform", "true", "upload"},
}

// validationErrorCode 把参数校验短名映射为规范错误码（短名仍保留在 error_type 中）。
func validationErrorCode(errType string) string {
	switch errType {
	case "invalid_ticket":
		return ErrPlugInvalidTicket
	case "invalid_module":
		return ErrPlugInvalidModule
	case "invalid_date":
		return ErrPlugInvalidDate
	case "invalid_ips":
		return ErrPlugInvalidIPs
	case "invalid_path":
		return ErrPlugInvalidPath
	case "invalid_remote_url":
		return ErrPlugInvalidURL
	case "invalid_action":
		return ErrPlugInvalidAction
	default:
		return ""
	}
}

// execErrorCode 根据执行错误归类：超时与 PTY 失败必须区分，前者可重试、后者不可。
func execErrorCode(err error, exitCode int) string {
	if err != nil && errors.Is(err, context.DeadlineExceeded) {
		return ErrExecTimeout
	}
	if exitCode > 0 {
		return ErrExecNonZero
	}
	if err != nil {
		return ErrExecWait
	}
	return ""
}

// logCollectErrorFields 返回可直接写入 OutMessage 的四个诊断字段。
// 未知码返回空值，调用方仍会保留 error_type 与 message，不阻断主流程。
func logCollectErrorFields(code string) (layer, owner, retryable string) {
	meta, ok := logCollectErrorMeta[code]
	if !ok {
		return "", "", ""
	}
	return meta.Layer, meta.Owner, meta.Retryable
}
