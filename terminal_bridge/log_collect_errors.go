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
	"strings"
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
	// 落盘校验不符：节点上的字节数与本地事实不一致（半包、被同名覆盖、中转污染）。
	// 必须与 004 区分：004 是"没传完"，007 是"传完了但不是同一个东西"。
	ErrInstallChecksum = "LC-INSTALL-007"
	// 下载不完整：实际收到的字节数与 Content-Length 不一致。
	ErrInstallTruncated = "LC-INSTALL-008"
	// 同一会话的安装包传输正在进行中：并发重试会各自写同一目标路径，
	// 历史现场因此出现过"包不对"。此码显式拒绝，而不是让它们互相覆盖。
	ErrInstallBusy = "LC-INSTALL-009"

	ErrExecPtyRequest = "LC-EXEC-001"
	ErrExecTimeout    = "LC-EXEC-002"
	ErrExecNonZero    = "LC-EXEC-003"
	ErrExecStart      = "LC-EXEC-004"
	ErrExecWait       = "LC-EXEC-005"

	// ── SPC：场景清单分发（create 之前的逐台写入）──────────────────────────
	// 为什么单独成层：失败发生在 bridge 自己的 SSH 写入闭环里，既不是 acli
	// 插件语义（PLUGIN），也不是平台服务（PLATFORM）。混进 EXEC-003 会让
	// 「节点 SSH 不通报错」与「acli 参数写错」共用一个码，用户无法判断该查网络
	// 还是查专项配置（2026-11-12 Q2026110302155 现场）。
	ErrSpcDirMissing     = "LC-SPC-001"
	ErrSpcDirAmbiguous   = "LC-SPC-002"
	ErrSpcBackupFailed   = "LC-SPC-003"
	ErrSpcWriteFailed    = "LC-SPC-004"
	ErrSpcRestoreFailed  = "LC-SPC-005"
	ErrSpcSSHUnreachable = "LC-SPC-006"
	ErrSpcSSHAuthFailed  = "LC-SPC-007"
	ErrSpcSSHTimeout     = "LC-SPC-008"
	ErrSpcFileMissing    = "LC-SPC-009"
	// 多台失败且原因不同时只能给聚合码；逐台原因在 Message 与 detail.spc_hosts 里。
	ErrSpcAggregate = "LC-SPC-010"

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
	// spc 清单备份按任务隔离，create 缺合法 exec_id 时桥会拒绝下发；
	// 此前该分支没有规范码，失败记录只剩文案，事后无法自动归因。
	ErrPlugInvalidTaskID = "LC-PLUGIN-014"
	// create 命令退出码为 0，但节点上始终没有任务。与 008（任务不存在/进度查不到）
	// 严格区分：008 是「查询到了一个不存在的任务」，本码是「下发之后根本没建立」，
	// 处置动作完全不同（前者核对任务 ID，后者核对 acli 版本与专项）。
	ErrPlugCreateNotEstablished = "LC-PLUGIN-015"
	// acli 明确回了「收集专项 X 不存在」：平台下发的专项名与节点 acli 内置清单不匹配。
	// 此前落 LC-EXEC-003（泛化非零退出），用户只看到「日志收集失败」，必须单独成码。
	ErrPlugModuleMissing = "LC-PLUGIN-016"

	ErrTransferArchiveMissing = "LC-TRANSFER-001"
	ErrTransferChecksum       = "LC-TRANSFER-002"
	ErrTransferChunkFailed    = "LC-TRANSFER-003"
	ErrTransferResumeFailed   = "LC-TRANSFER-004"
	ErrTransferTooLarge       = "LC-TRANSFER-005"
	// 探测命令本身跑不了（命令不存在/通道异常）：不能说包没了，只能说无法确认。
	// 与 001（包确定不存在）严格区分：混用会让平台对着盘上的包报“已被删除”。
	ErrTransferArchiveProbe = "LC-TRANSFER-006"
	// 客户机本地盘写满：P2 中转必须先整包落地再分片上传，盘满时一个字节都传不出去。
	// 必须与 003（分片上传失败）区分：003 重试可能成功，008 重试只会再写满一次；
	// 也要与 ENV-004（节点侧 /sf/data/local 不足）区分：那是 HCI 节点，这是客户机。
	// 码号避让：LC-TRANSFER-007 已被 main 的 ErrTransferArchiveUnverified 占用。
	ErrTransferLocalDiskFull = "LC-TRANSFER-008"
	// 并发准入：同盘还有其它交付作业未落地，叠加后本地空间不够本次交付。
	// 与 008 的区别：008 是"这台机器本身就没空间了"，清盘即可；
	// 009 是"空间被别的交付作业占着"，等它完成即可，清盘往往无济于事——
	// 混用会把用户引向错误的处置动作（2026-11-03 并发叠加场景）。
	ErrTransferAdmissionExceeded = "LC-TRANSFER-009"
	// 源包仍在写入：acli 进度到 100% 但 zip 未收尾，或提交后包被重新生成。
	// 与 003（分片上传失败）的区别：003 重试有机会成功，010 重试只会追一个
	// 移动的目标；处置动作是"等包大小稳定后重开一次上传会话"。
	ErrTransferSourceStillWriting = "LC-TRANSFER-010"
	// P1 直传后按产物名回查 5888 归档未通过（不存在/大小不一致/时间窗外）。
	// 与 001/006 的区别：001 是包没生成，006 是探测不了，本码是包生成了、
	// 也（据 acli 说）传完了，但落盘侧没有一致的归档件。
	ErrTransferArchiveUnverified = "LC-TRANSFER-007"

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
	// 与 bridge 的通道断开、节点操作未送达。区别于 EXEC-005（命令已下发但没取回结果）：
	// 本码意味着节点侧没有留下任何任务，重连后重试是安全的。
	ErrPlatChannelLost = "LC-PLATFORM-010"
	// 失败已确认但 error_message 匹配不到任何已知模式（平台保存边界的兜底码）。
	// 它不是"未知错误"的同义词：留空会让失败无法聚合统计，兜底码本身是可观测信号。
	ErrPlatUnattributed = "LC-PLATFORM-011"
	// 平台内部 5xx（非存储、非数据库专项）。区分于 011：原因已知（是我们自己 500），
	// 只是没有更细的分层码，因此可自动重试。
	ErrPlatInternal = "LC-PLATFORM-012"
	// 采集已终态、交付会话却从未被执行（僵尸会话）。区别于 011：这是确定的
	// 交付缺失事实，处置动作明确（补交付），不需要人工归因。
	ErrPlatDeliveryAbandoned = "LC-PLATFORM-013"
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
	ErrInstallChecksum:  {"INSTALL", "customer", "true", "verify"},
	ErrInstallTruncated: {"INSTALL", "customer", "true", "install"},
	ErrInstallBusy:      {"INSTALL", "platform", "true", "install"},

	ErrExecPtyRequest: {"EXEC", "customer", "false", "exec"},
	ErrExecTimeout:    {"EXEC", "customer", "true", "exec"},
	ErrExecNonZero:    {"EXEC", "product", "false", "exec"},
	ErrExecStart:      {"EXEC", "customer", "true", "exec"},
	ErrExecWait:       {"EXEC", "customer", "true", "exec"},

	ErrSpcDirMissing:     {"SPC", "customer", "manual", "create"},
	ErrSpcDirAmbiguous:   {"SPC", "customer", "manual", "create"},
	ErrSpcBackupFailed:   {"SPC", "customer", "manual", "create"},
	ErrSpcWriteFailed:    {"SPC", "customer", "manual", "create"},
	ErrSpcRestoreFailed:  {"SPC", "customer", "manual", "create"},
	ErrSpcSSHUnreachable: {"SPC", "customer", "manual", "create"},
	ErrSpcSSHAuthFailed:  {"SPC", "customer", "manual", "create"},
	ErrSpcSSHTimeout:     {"SPC", "customer", "true", "create"},
	ErrSpcFileMissing:    {"SPC", "customer", "manual", "create"},
	ErrSpcAggregate:      {"SPC", "customer", "manual", "create"},

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
	ErrPlugInvalidTaskID: {"PLUGIN", "platform", "false", "create"},
	// 未建立任务的归 customers：节点 acli 版本/专项是客户侧可核对的。
	ErrPlugCreateNotEstablished: {"PLUGIN", "customer", "manual", "progress"},
	ErrPlugModuleMissing:        {"PLUGIN", "customer", "manual", "create"},

	ErrTransferArchiveMissing: {"TRANSFER", "customer", "manual", "package"},
	ErrTransferChecksum:       {"TRANSFER", "platform", "true", "upload"},
	ErrTransferChunkFailed:    {"TRANSFER", "customer", "true", "upload"},
	ErrTransferResumeFailed:   {"TRANSFER", "platform", "manual", "upload"},
	ErrTransferTooLarge:       {"TRANSFER", "platform", "false", "upload"},
	ErrTransferArchiveProbe:   {"TRANSFER", "customer", "true", "package"},
	// 磁盘满不可自动重试：清盘是人做的事，重试只会在同一位置再失败一次。
	ErrTransferLocalDiskFull: {"TRANSFER", "customer", "manual", "package"},
	// 等其它作业完成即可，不是人手工清盘能解决的；重试同样不会自己变好。
	ErrTransferAdmissionExceeded: {"TRANSFER", "customer", "manual", "package"},
	// 等包写完即可恢复；这不是网络抖动，在同一个会话上重试不会变好。
	ErrTransferSourceStillWriting: {"TRANSFER", "customer", "manual", "package"},
	// 不可自动重试：P1 成功后插件已删源包，重试没有输入。
	ErrTransferArchiveUnverified: {"TRANSFER", "customer", "false", "upload"},

	ErrPlatUnauthorized: {"PLATFORM", "platform", "true", "api"},
	ErrPlatForbidden:    {"PLATFORM", "platform", "false", "api"},
	ErrPlatTransition:   {"PLATFORM", "platform", "false", "api"},
	ErrPlatRegression:   {"PLATFORM", "platform", "false", "api"},
	ErrPlatDuplicate:    {"PLATFORM", "platform", "manual", "api"},
	ErrPlatDBDown:       {"PLATFORM", "platform", "true", "api"},

	ErrPlatStorageUnwritable: {"PLATFORM", "platform", "manual", "upload"},
	ErrPlatStorageLow:        {"PLATFORM", "platform", "true", "upload"},
	// 通道断开可重试：节点侧没有留下任务，重连后重发不会产生重复副作用。
	ErrPlatChannelLost: {"PLATFORM", "platform", "true", "api"},
	// 未归因属于平台责任：需要平台侧按 trace_id 回溯补码，不能指望用户重试解决。
	ErrPlatUnattributed: {"PLATFORM", "platform", "manual", "api"},
	// 平台 5xx 多为瞬时：退避重试即可，用户侧无需额外操作。
	ErrPlatInternal: {"PLATFORM", "platform", "true", "api"},
	// 交付缺失但产物通常仍在：补交付可重试，不需要重新采集。
	ErrPlatDeliveryAbandoned: {"PLATFORM", "platform", "true", "api"},
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
	case "invalid_task_id":
		return ErrPlugInvalidTaskID
	// SPC 扇出的内部短名 → 规范码。短名仍写进 error_type，便于按「哪一步失败」聚合。
	case "spc_dir_missing":
		return ErrSpcDirMissing
	case "spc_dir_ambiguous":
		return ErrSpcDirAmbiguous
	case "backup_failed", "backup_mismatch":
		return ErrSpcBackupFailed
	case "write_failed", "write_rejected", "verify_mismatch":
		return ErrSpcWriteFailed
	case "spc_file_missing":
		return ErrSpcFileMissing
	case "host_unreachable":
		return ErrSpcSSHUnreachable
	case "host_auth_failed":
		return ErrSpcSSHAuthFailed
	case "host_timeout":
		return ErrSpcSSHTimeout
	case "spc_restore_failed":
		return ErrSpcRestoreFailed
	case "spc_write_failed":
		return ErrSpcAggregate
	case "spc_module_missing":
		return ErrPlugModuleMissing
	case "create_not_established":
		return ErrPlugCreateNotEstablished
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

// spcErrorTitle 给出可直接展示的中文短语。
//
// 标题一律取自契约生成码表（唯一出处）：短名 → 规范码由 validationErrorCode 负责，
// 未知短名回退到聚合码。原先这里手写 13 条中文，与平台诊断卡、管理端归因各写一份，
// 是「同码不同说法」的根源；删掉后桥侧不再维护任何展示文案。
func spcErrorTitle(errType string) string {
	if title := contractTitle(validationErrorCode(errType)); title != "" {
		return title
	}
	return contractTitle(ErrSpcAggregate)
}

// SPC 执行步骤：决定「没有传输层特征」的失败该归到哪一步。
// 远端命令非零退出（cp 失败、只读文件系统、权限不足）不是连接问题，
// 一律说成「节点不可达」会把用户引去查网络，而真正要修的是文件系统与权限。
const (
	spcStepDir     = "dir"
	spcStepBackup  = "backup"
	spcStepWrite   = "write"
	spcStepRestore = "restore"
)

// spcStepErrorType 返回某一步骤自身的失败短名（transport 类错误不会走到这里）。
func spcStepErrorType(step string) string {
	switch step {
	case spcStepBackup:
		return "backup_failed"
	case spcStepWrite:
		return "write_rejected"
	case spcStepRestore:
		return "spc_restore_failed"
	default:
		return "spc_dir_missing"
	}
}

// spcJoinDiag 把 stdout 与 stderr 合成诊断文本（仅用于归因，不用于解析）。
func spcJoinDiag(stdout, stderr string) string {
	switch {
	case stderr == "":
		return stdout
	case stdout == "":
		return stderr
	default:
		return stdout + "\n" + stderr
	}
}

// classifySpcCommandError 把 SSH/远程命令错误细分到可处置的粒度。
//
// 三类判据，顺序不可颠倒：
//  1. 传输层（SSH 本身失败）：认证 / 超时 / 不可达；
//  2. 文件系统与权限（远端命令失败）：只读、空间不足、权限不足 → 归到「这一步」；
//  3. 都不像：只有明确是 ssh 客户端输出才算连接失败，其余按步骤归因。
//
// 第 3 条是本次修复的重点：旧实现 default 直接给「节点不可达」，
// 于是「只读文件系统写不进去」也会提示用户去查网络（2026-11-12 现场）。
func classifySpcCommandError(err error, diag, step string) (errType, detail string) {
	detail = spcErrorDetail(diag, err)
	text := strings.ToLower(errString(err) + "\n" + diag)
	switch {
	case err != nil && errors.Is(err, context.DeadlineExceeded):
		return "host_timeout", detail
	case strings.Contains(text, "unable to authenticate"),
		strings.Contains(text, "no supported methods"),
		strings.Contains(text, "permission denied (publickey"):
		return "host_auth_failed", detail
	case strings.Contains(text, "i/o timeout"),
		strings.Contains(text, "connection timed out"),
		strings.Contains(text, "deadline exceeded"):
		return "host_timeout", detail
	case strings.Contains(text, "connection refused"),
		strings.Contains(text, "no route to host"),
		strings.Contains(text, "network is unreachable"),
		strings.Contains(text, "connection reset"),
		strings.Contains(text, "host key verification failed"),
		strings.Contains(text, "kex_exchange_identification"),
		strings.Contains(text, "connection closed by remote host"):
		return "host_unreachable", detail
	case strings.Contains(text, "no space left on device"),
		strings.Contains(text, "read-only file system"),
		strings.Contains(text, "disk quota exceeded"),
		strings.Contains(text, "operation not permitted"),
		strings.Contains(text, "permission denied"),
		strings.Contains(text, "input/output error"):
		return spcStepErrorType(step), detail
	case strings.Contains(text, "no such file or directory"):
		return "spc_file_missing", detail
	}
	if strings.Contains(text, "ssh:") || strings.Contains(text, "dial tcp") || strings.Contains(text, "handshake failed") {
		return "host_unreachable", detail
	}
	return spcStepErrorType(step), detail
}

// spcErrorDetail 组合「错误原文 + 输出尾部」，最多 400 字。
// 之前只把 error_code 写进 detail.spc_hosts，stderr 在内存里丢失，
// 现场只能看到"节点不可达"，无法判断是免密、路径还是权限。
func spcErrorDetail(output string, err error) string {
	parts := make([]string, 0, 2)
	if s := strings.TrimSpace(errString(err)); s != "" {
		parts = append(parts, s)
	}
	if s := spcOutputTail(output, 3, 200); s != "" {
		parts = append(parts, s)
	}
	return truncateString(strings.Join(parts, " | "), 400)
}

// spcOutputTail 取输出末尾若干行，跳过空行与标记行。
// 标记行（SPC_WRITE_FAIL reason=...）本身没有诊断价值，留下只会挤掉 stderr。
func spcOutputTail(output string, maxLines, maxLen int) string {
	lines := strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n")
	picked := make([]string, 0, maxLines)
	for i := len(lines) - 1; i >= 0 && len(picked) < maxLines; i-- {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "SPC_") {
			continue
		}
		picked = append([]string{truncateString(line, maxLen)}, picked...)
	}
	return strings.Join(picked, " / ")
}
