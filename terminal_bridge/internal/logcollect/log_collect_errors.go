package logcollect

// SPC 场景清单下发的失败归因（唯一职责：把一次远端失败归到可处置的步骤）。
//
// 迁移说明（阶段 2 A3）：LC-SPC-* / LC-SCRIPT-* 等错误码常量与元数据已迁到
// shared/logcontract —— 它们是跨域契约，不是 logcollect 的私有实现。
// 本文件只保留依赖远端输出语义的判定逻辑（含 sshx 依赖），因此留在业务域。

import (
	"context"
	"errors"
	"strings"

	"terminal_bridge/internal/shared/logcontract"
	"terminal_bridge/internal/sshx"
)

// SpcErrorTitle 给出可直接展示的中文短语。
//
// 标题一律取自契约生成码表（唯一出处）：短名 → 规范码由 ValidationErrorCode 负责，
// 未知短名回退到聚合码。原先这里手写 13 条中文，与平台诊断卡、管理端归因各写一份，
// 是「同码不同说法」的根源；删掉后桥侧不再维护任何展示文案。
func SpcErrorTitle(errType string) string {
	if title := logcontract.Title(logcontract.ValidationErrorCode(errType)); title != "" {
		return title
	}
	return logcontract.Title(logcontract.ErrSpcAggregate)
}

// SPC 执行步骤：决定「没有传输层特征」的失败该归到哪一步。
// 远端命令非零退出（cp 失败、只读文件系统、权限不足）不是连接问题，
// 一律说成「节点不可达」会把用户引去查网络，而真正要修的是文件系统与权限。
const (
	SpcStepDir     = "dir"
	SpcStepBackup  = "backup"
	SpcStepWrite   = "write"
	SpcStepRestore = "restore"
)

// spcStepErrorType 返回某一步骤自身的失败短名（transport 类错误不会走到这里）。
func spcStepErrorType(step string) string {
	switch step {
	case SpcStepBackup:
		return "backup_failed"
	case SpcStepWrite:
		return "write_rejected"
	case SpcStepRestore:
		return "spc_restore_failed"
	default:
		return "spc_dir_missing"
	}
}

// SpcJoinDiag 把 stdout 与 stderr 合成诊断文本（仅用于归因，不用于解析）。
func SpcJoinDiag(stdout, stderr string) string {
	switch {
	case stderr == "":
		return stdout
	case stdout == "":
		return stderr
	default:
		return stdout + "\n" + stderr
	}
}

// ClassifySpcCommandError 把 SSH/远程命令错误细分到可处置的粒度。
//
// 三类判据，顺序不可颠倒：
//  1. 传输层（SSH 本身失败）：认证 / 超时 / 不可达；
//  2. 文件系统与权限（远端命令失败）：只读、空间不足、权限不足 → 归到「这一步」；
//  3. 都不像：只有明确是 ssh 客户端输出才算连接失败，其余按步骤归因。
//
// 第 3 条是本次修复的重点：旧实现 default 直接给「节点不可达」，
// 于是「只读文件系统写不进去」也会提示用户去查网络（2026-11-12 现场）。
func ClassifySpcCommandError(err error, diag, step string) (errType, detail string) {
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
	if s := SpcOutputTail(output, 3, 200); s != "" {
		parts = append(parts, s)
	}
	return sshx.TruncateString(strings.Join(parts, " | "), 400)
}

// SpcOutputTail 取输出末尾若干行，跳过空行与标记行。
// 标记行（SPC_WRITE_FAIL reason=...）本身没有诊断价值，留下只会挤掉 stderr。
func SpcOutputTail(output string, maxLines, maxLen int) string {
	lines := strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n")
	picked := make([]string, 0, maxLines)
	for i := len(lines) - 1; i >= 0 && len(picked) < maxLines; i-- {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "SPC_") {
			continue
		}
		picked = append([]string{sshx.TruncateString(line, maxLen)}, picked...)
	}
	return strings.Join(picked, " / ")
}
