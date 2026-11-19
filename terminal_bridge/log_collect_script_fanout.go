package main

// 收集脚本扇出与终态恢复（单片原语见 log_collect_script.go，本文件只做编排）。
//
// 与 spc 扇出同构，复用 reconcile.Fanout / spcTaskState / 逐台证据结构：
// 写文件、按主机串行、终态还原这三件事的语义完全一致，另起一套只会让两边的
// 失败码与还原时机各自演化。
//
// 差异只有三处，均已在本文件显式处理：
//  1. 目标目录是 script/（spc 是 spc/）；
//  2. 文件名是带扩展名的 code（spc 是 <slot>.json）；
//  3. 节点上可能原本没有这个文件（平台新增脚本），因此备份之外还有 created 标记。

import (
	"context"
	"fmt"
	"strings"
	"terminal_bridge/internal/app"
	"time"

	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/sshx"
)

// scriptApplySpec 是写入所需的全部输入。
type scriptApplySpec struct {
	TaskID string
	Files  []scriptFile
}

// prepareScriptScenario 完成「解析目标主机 + 逐台写入 + 全量校验」，成功才返回现场。
// 任何一台写不进去都回滚并拒绝：节点之间跑不同版本的脚本，产物无法解释。
func prepareScriptScenario(b *app.Bridge,
	ctx context.Context, s *sshx.SSHSession, msg models.InMessage, traceID, customUI string,
) (*spcTaskState, *spcRejection) {
	files, rejection := parseScriptFiles(msg)
	if rejection != nil {
		return nil, rejection
	}
	if len(files) == 0 {
		return nil, nil
	}
	taskID := strings.TrimSpace(msg.ExecID)
	if !spcTaskIDRe.MatchString(taskID) {
		return nil, &spcRejection{ErrType: "invalid_task_id", Message: "缺少合法的任务 ID，无法确定脚本备份归属"}
	}

	var hosts []logCollectHostOption
	if value := strings.TrimSpace(msg.IPs); value == "" || strings.EqualFold(value, "all") {
		hosts = spcNodeList(s, ctx)
	}
	targets, all, err := spcTargetSet(msg.IPs, hosts)
	if err != nil {
		return nil, &spcRejection{ErrType: "script_hosts_unknown", Message: err.Error()}
	}

	codes := make([]string, 0, len(files))
	digests := make([]string, 0, len(files))
	totalBytes := 0
	for _, file := range files {
		codes = append(codes, file.Code)
		digests = append(digests, file.Code+":"+file.SHA256)
		totalBytes += len(file.Content)
	}
	// Slot/Slots 复用 spcTaskState 的槽位字段承载脚本文件名：
	// 还原时按同一字段逐份重放，不必为脚本再定义一套状态机。
	state := &spcTaskState{
		TaskID:       taskID,
		Slot:         codes[0],
		Slots:        codes,
		Targets:      targets,
		LocalIPs:     spcLocalAddresses(s, ctx),
		CreatedAt:    time.Now(),
		restoreState: "pending",
		activator:    reconcile.NewActivator(),
	}
	logpush.Blog("INFO", "log_collect.script_fanout_start", "开始把收集脚本写入目标节点", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
		"task_id": taskID, "codes": codes, "digests": digests,
		"hosts_count": len(targets), "ips_all": all, "script_bytes": totalBytes,
	})

	results, writtenSet := applyScriptAll(b, ctx, s, state, scriptApplySpec{TaskID: taskID, Files: files})
	state.setHosts(results)
	state.written = writtenSet

	written, failed := spcClassifyHosts(results)
	// all 模式下不可达视为本轮不参与（与 spc 一致）；点名模式下任何一台失败都拒绝。
	blocking := failed
	if all {
		blocking = make([]spcHostState, 0, len(failed))
		for _, host := range failed {
			if host.ErrorCode != spcErrUnreachable {
				blocking = append(blocking, host)
			}
		}
	}
	if len(blocking) > 0 || written == 0 {
		report := blocking
		if len(report) == 0 {
			report = failed
		}
		errType, code := scriptRejectionCode(report)
		rejection := &spcRejection{
			ErrType: errType,
			Code:    code,
			Message: scriptRejectionMessage(written, len(state.Targets), report),
		}
		restored, restoreFailed := rollbackScriptAll(b, s, state, spcWrittenTargets(results))
		if restoreFailed > 0 {
			rejection.Message = sshx.TruncateString(rejection.Message+
				contractFormat("script_restore_risk", map[string]string{"count": fmt.Sprint(restoreFailed)}), 560)
		}
		logpush.Blog("ERROR", "log_collect.script_fanout_failed", "脚本写入未通过，已回滚并拒绝下发", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
			"task_id": taskID, "codes": codes, "written": written,
			"error_code": code, "error_type": errType,
			"failed_hosts":   spcHostAuditTrail(blocking),
			"restored_hosts": restored,
			"restore_failed": restoreFailed,
			"reason":         rejection.Message,
		})
		return nil, rejection
	}

	spcTaskStore.put(state)
	logpush.Blog("INFO", "log_collect.script_fanout", "收集脚本已写入全部目标节点", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
		"task_id": taskID, "codes": codes, "written": written,
		"failed_hosts": spcHostAuditTrail(failed), "hosts": spcHostAuditTrail(results),
	})
	return state, nil
}

// scriptRejectionCode 把逐台根因收敛成一个主码：码决定用户下一步查什么。
func scriptRejectionCode(failed []spcHostState) (string, string) {
	seen := map[string]bool{}
	for _, host := range failed {
		seen[host.ErrorCode] = true
	}
	if len(seen) == 1 {
		for code := range seen {
			switch code {
			case spcErrUnreachable:
				return code, ErrSpcSSHUnreachable
			case "script_dir_missing":
				return code, ErrScriptDirMissing
			case "script_backup_failed":
				return code, ErrScriptBackupFailed
			case "script_write_failed":
				return code, ErrScriptWriteFailed
			case spcErrBusy:
				return code, ErrSpcHostBusy
			}
		}
	}
	return "script_aggregate", ErrScriptAggregate
}

func scriptRejectionMessage(written, total int, failed []spcHostState) string {
	var builder strings.Builder
	fmt.Fprintf(&builder, "收集脚本写入未通过：目标 %d 台，成功 %d 台", total, written)
	for _, host := range failed {
		fmt.Fprintf(&builder, "；%s %s", host.IP, firstNonEmpty(host.ErrorDetail, host.ErrorCode))
	}
	return sshx.TruncateString(builder.String(), 560)
}

// applyScriptAll 并发把脚本写到全部目标节点（并发上限与主机锁沿用 spcWriteFanout，
// 保证同一台主机上「场景写入」与「脚本写入」不会交错）。
func applyScriptAll(b *app.Bridge,
	ctx context.Context, s *sshx.SSHSession, state *spcTaskState, spec scriptApplySpec,
) ([]spcHostState, reconcile.WrittenSet) {
	results, written := spcWriteFanout().WriteAll(ctx, state.Targets, func(ctx context.Context, ip string) reconcile.HostResult {
		host := applyScriptToHost(b, ctx, s, spec, ip, state.LocalIPs[ip], state.activator)
		return reconcile.HostResult{
			Host: host.IP, Written: host.Written,
			ErrorCode: host.ErrorCode, ErrorDetail: host.ErrorDetail, Value: host,
		}
	})
	hosts := make([]spcHostState, 0, len(results))
	for _, result := range results {
		hosts = append(hosts, spcHostFromResult(result))
	}
	return hosts, written
}

// applyScriptToHost 在单台主机上完成目录探测 → 逐脚本备份 → 写入 → 落盘校验。
func applyScriptToHost(b *app.Bridge,
	ctx context.Context, s *sshx.SSHSession, spec scriptApplySpec, ip string, local bool, activator *reconcile.Activator,
) spcHostState {
	state := spcHostState{IP: ip, RestoreState: "pending"}

	output, errOut, _, err := runSpcOnHostStreams(s, ctx, ip, local, buildScriptDirProbe())
	if err != nil {
		state.ErrorCode, state.ErrorDetail = classifySpcCommandError(err, spcJoinDiag(output, errOut), spcStepDir)
		return state
	}
	dir, dirCode := scriptResolveDir(output)
	if dirCode != "" {
		state.ErrorCode = dirCode
		state.ErrorDetail = "固定脚本目录不存在且无法创建（acli 日志收集插件可能未安装）"
		return state
	}
	state.Dir = dir

	for _, file := range spec.Files {
		fileState := applyScriptFileToHost(b, ctx, s, ip, local, dir, spec.TaskID, file, activator)
		if len(spec.Files) > 1 {
			state.Slots = append(state.Slots, fileState)
		}
		if len(state.Slots) <= 1 {
			state.Path, state.Backup, state.OriginalSHA = fileState.Path, fileState.Backup, fileState.OriginalSHA
		}
		if fileState.OriginalSHA != "" {
			state.Prepared = true
		}
		if fileState.ErrorCode != "" {
			state.ErrorCode = fileState.ErrorCode
			state.ErrorDetail = fmt.Sprintf("脚本 %s：%s", file.Code, firstNonEmpty(fileState.ErrorDetail, fileState.ErrorCode))
			return state
		}
	}
	state.Written = true
	return state
}

// applyScriptFileToHost 在已探测到的目录里完成一份脚本的完整写入闭环。
func applyScriptFileToHost(b *app.Bridge,
	ctx context.Context, s *sshx.SSHSession, ip string, local bool, dir, taskID string,
	file scriptFile, activator *reconcile.Activator,
) spcHostSlotState {
	state := spcHostSlotState{Slot: file.Code}
	target, pathErr := scriptFilePath(dir, file.Code)
	if pathErr != nil {
		state.ErrorCode = spcErrFileMissing
		state.ErrorDetail = pathErr.Error()
		return state
	}
	backup, backupErr := scriptBackupPath(target, taskID)
	if backupErr != nil {
		state.ErrorCode = "script_backup_failed"
		state.ErrorDetail = backupErr.Error()
		return state
	}
	marker := target + ".platform." + taskID + ".created"
	state.Path, state.Backup = target, backup

	prepareOut, prepareErrOut, _, prepareErr := runSpcOnHostStreams(s, ctx, ip, local, buildScriptPrepareCommand(target, backup, marker))
	if prepareErr != nil {
		state.ErrorCode, state.ErrorDetail = classifySpcCommandError(prepareErr, spcJoinDiag(prepareOut, prepareErrOut), spcStepBackup)
		state.ErrorCode = "script_backup_failed"
		return state
	}
	if !spcMarkerHas(prepareOut, "SCRIPT_PREPARE_OK") {
		state.ErrorCode = "script_backup_failed"
		state.ErrorDetail = spcOutputTail(spcJoinDiag(prepareOut, prepareErrOut), 2, 200)
		return state
	}
	state.OriginalSHA = scriptMarkerField(prepareOut, "orig")

	commands := buildScriptWriteCommands(target, backup, marker, file.Content)
	for index, command := range commands {
		writeOut, writeErrOut, _, writeErr := runSpcOnHostStreams(s, ctx, ip, local, command)
		if writeErr != nil {
			state.ErrorCode, state.ErrorDetail = classifySpcCommandError(writeErr, spcJoinDiag(writeOut, writeErrOut), spcStepWrite)
			state.ErrorCode = "script_write_failed"
			return state
		}
		if reason := scriptFailureReason(writeOut); reason != "" {
			state.ErrorCode = "script_write_failed"
			state.ErrorDetail = fmt.Sprintf("第 %d/%d 块 %s；%s", index+1, len(commands), reason,
				spcOutputTail(spcJoinDiag(writeOut, writeErrOut), 2, 200))
			return state
		}
		if index == len(commands)-1 && !spcMarkerHas(writeOut, "__ACLI_WRITE_OK") {
			state.ErrorCode = "script_write_failed"
			state.ErrorDetail = "落盘校验未通过（未拿到写入成功标记）"
			return state
		}
	}
	state.Written = true
	// 只有真正写成功才进入可回滚集合：备份名按任务隔离。
	activator.Register(reconcile.WriteEntry{Path: target, BackupPath: backup})
	return state
}

// scriptMarkerField 从 SCRIPT_ 开头的回显行里取 key=value。
func scriptMarkerField(output, key string) string {
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r", ""), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "SCRIPT_") {
			continue
		}
		for _, field := range strings.Fields(line) {
			if parts := strings.SplitN(field, "=", 2); len(parts) == 2 && parts[0] == key {
				return parts[1]
			}
		}
	}
	return ""
}

// scriptFailureReason 汇总写入失败原因：既认本层 SCRIPT_WRITE_FAIL，也认 remote 写入协议的标记。
func scriptFailureReason(output string) string {
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r", ""), "\n") {
		line = strings.TrimSpace(line)
		if strings.Contains(line, "SCRIPT_WRITE_FAIL") {
			for _, field := range strings.Fields(line) {
				if strings.HasPrefix(field, "reason=") {
					return strings.TrimPrefix(field, "reason=")
				}
			}
			return "write_failed"
		}
		if strings.Contains(line, "__ACLI_WRITE_FAIL") {
			reason := strings.TrimSpace(line[strings.Index(line, "__ACLI_WRITE_FAIL")+len("__ACLI_WRITE_FAIL"):])
			if reason == "" {
				reason = "write_failed"
			}
			return reason
		}
	}
	return ""
}

// rollbackScriptAll 回滚本次已写入的节点（拒绝路径专用，幂等）。
func rollbackScriptAll(b *app.Bridge, s *sshx.SSHSession, st *spcTaskState, targets []string) (int, int) {
	if st == nil || len(targets) == 0 {
		return 0, 0
	}
	ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
	defer cancel()
	results := restoreScriptHosts(b, ctx, s, spcRestorePlan{
		Slot: st.Slot, Slots: st.Slots, TaskID: st.TaskID, Targets: targets,
		LocalIPs: st.LocalIPs, Dirs: st.dirs(),
	})
	return spcCountRestore(results)
}

// restoreScriptHosts 并发恢复全部目标节点（不占主机锁：还原是幂等 cp/rm）。
func restoreScriptHosts(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, plan spcRestorePlan) []spcHostState {
	results := spcRestoreFanout().Each(ctx, plan.Targets, func(ctx context.Context, ip string) reconcile.HostResult {
		return spcRestoreResult(restoreScriptOnHost(b, ctx, s, plan, ip))
	})
	hosts := make([]spcHostState, 0, len(results))
	for _, result := range results {
		hosts = append(hosts, spcRestoreHostFromResult(result))
	}
	return hosts
}

func restoreScriptOnHost(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, plan spcRestorePlan, ip string) spcHostState {
	state := spcHostState{IP: ip}
	local := plan.LocalIPs[ip]
	dir := plan.Dirs[ip]
	if dir == "" {
		dir = scriptDeployDir
	}
	state.Dir = dir

	codes := plan.Slots
	if len(codes) == 0 && plan.Slot != "" {
		codes = []string{plan.Slot}
	}
	restored, skipped, failed := 0, 0, 0
	firstErr := ""
	for _, code := range codes {
		fileState := restoreScriptFileOnHost(b, ctx, s, ip, local, dir, plan.TaskID, code)
		if len(codes) > 1 {
			state.Slots = append(state.Slots, fileState)
		}
		if len(state.Slots) <= 1 {
			state.Path, state.Backup = fileState.Path, fileState.Backup
		}
		switch fileState.RestoreState {
		case "restored":
			restored++
		case "skipped":
			skipped++
		default:
			failed++
			if firstErr == "" {
				firstErr = firstNonEmpty(fileState.RestoreError, ErrScriptRestoreFailed)
				state.ErrorDetail = fileState.ErrorDetail
			}
		}
	}
	switch {
	case failed > 0:
		state.RestoreState = "failed"
		state.RestoreError = firstErr
	case restored > 0:
		state.RestoreState = "restored"
	default:
		state.RestoreState = "skipped"
	}
	return state
}

// restoreScriptFileOnHost 恢复一份脚本：备份不存在返回 skipped，幂等重放不误报。
func restoreScriptFileOnHost(b *app.Bridge,
	ctx context.Context, s *sshx.SSHSession, ip string, local bool, dir, taskID, code string,
) spcHostSlotState {
	state := spcHostSlotState{Slot: code}
	target, pathErr := scriptFilePath(dir, code)
	if pathErr != nil {
		state.RestoreState = "failed"
		state.RestoreError = spcErrFileMissing
		state.ErrorDetail = pathErr.Error()
		return state
	}
	backup, backupErr := scriptBackupPath(target, taskID)
	if backupErr != nil {
		state.RestoreState = "failed"
		state.RestoreError = "script_backup_failed"
		state.ErrorDetail = backupErr.Error()
		return state
	}
	marker := target + ".platform." + taskID + ".created"
	state.Path, state.Backup = target, backup

	output, errOut, _, err := runSpcOnHostStreams(s, ctx, ip, local, buildScriptRestoreCommand(target, backup, marker))
	if err != nil {
		state.RestoreState = "failed"
		state.RestoreError = "script_restore_failed"
		state.ErrorDetail = spcOutputTail(spcJoinDiag(output, errOut), 2, 200)
		return state
	}
	switch {
	case spcMarkerHas(output, "SCRIPT_RESTORE_OK"):
		state.RestoreState = "restored"
	case spcMarkerHas(output, "SCRIPT_RESTORE_SKIP"):
		state.RestoreState = "skipped"
	default:
		state.RestoreState = "failed"
		state.RestoreError = "script_restore_failed"
		state.ErrorDetail = spcOutputTail(spcJoinDiag(output, errOut), 2, 200)
	}
	return state
}

// watchScriptRestore 在任务终态把脚本换回原件。
//
// 与 spc 一样不把恢复完全交给平台：平台轮询只在页面打开时发生，
// bridge 是常驻进程，才是节点文件状态的最终责任方。
func watchScriptRestore(b *app.Bridge, s *sshx.SSHSession, st *spcTaskState, traceID, caseID, nodeIP, customUI string) {
	go func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				logpush.Blog("ERROR", "log_collect.script_restore_panic", "脚本恢复协程异常退出", traceID, caseID, nodeIP, customUI, map[string]any{
					"task_id": st.TaskID, "panic": fmt.Sprint(recovered),
				})
			}
		}()
		deadline := time.Now().Add(spcRestoreMaxWait)
		idlePolls, failPolls := 0, 0
		for {
			time.Sleep(spcRestorePollInterval)
			if s == nil || s.IsClosed() {
				restoreScriptTask(b, s, st, "session_closed", traceID, caseID, nodeIP, customUI)
				return
			}
			s.MarkBusy()
			pollCtx, cancel := context.WithTimeout(context.Background(), logCollectProgressTimeout)
			output, exitCode, err := s.RunQuickCommand(pollCtx, logCollectAcliInvoke("log_collect progress"))
			cancel()
			if err != nil || exitCode != 0 {
				failPolls++
				if failPolls >= spcRestoreFailPolls {
					logpush.Blog("WARN", "log_collect.script_restore_abandon", "进度连续不可用，脚本恢复交给平台兜底", traceID, caseID, nodeIP, customUI, map[string]any{
						"task_id": st.TaskID, "error": errString(err), "exit_code": exitCode,
					})
					return
				}
				continue
			}
			failPolls = 0
			progress := parseLogCollectProgress(output)
			nodeState := classifyLogCollectNodeState(progress)
			st.noteState(nodeState)
			if nodeState == logCollectNodeNone {
				idlePolls++
			} else {
				idlePolls = 0
			}
			if due, reason := spcRestoreDue(nodeState, st.hasRun(), idlePolls); due {
				restoreScriptTask(b, s, st, reason, traceID, caseID, nodeIP, customUI)
				return
			}
			if time.Now().After(deadline) {
				restoreScriptTask(b, s, st, "timeout", traceID, caseID, nodeIP, customUI)
				return
			}
		}
	}()
}

// restoreScriptTask 执行一次脚本还原并落日志；beginRestore 保证同任务只跑一次。
func restoreScriptTask(b *app.Bridge, s *sshx.SSHSession, st *spcTaskState, reason, traceID, caseID, nodeIP, customUI string) {
	if st == nil || !st.beginRestore() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
	defer cancel()
	// 只还原真正写入过的节点：没写过的节点若「无备份但目标存在」，
	// 按 created 语义会被当成平台新建文件删掉，那是误删用户文件。
	results := restoreScriptHosts(b, ctx, s, spcRestorePlan{
		Slots: st.Slots, TaskID: st.TaskID, Targets: st.writtenTargets(),
		LocalIPs: st.LocalIPs, Dirs: st.dirs(),
	})
	restored, failed := spcCountRestore(results)
	status := "success"
	level := "INFO"
	if failed > 0 {
		status = "failed"
		level = "WARN"
	}
	st.finishRestore(status, reason, results)
	logpush.Blog(level, "log_collect.script_restore", "收集脚本恢复结果", traceID, caseID, nodeIP, customUI, map[string]any{
		"task_id": st.TaskID, "codes": st.Slots, "trigger": reason,
		"restored": restored, "failed": failed, "hosts_count": len(results),
		"hosts": spcHostAuditTrail(results),
	})
}

// handleScriptRestore 处理平台显式发起的 restore_script（bridge 重启后的兜底）。
// 内存态可能已经没了：仍按「固定目录 + 拼备份名」恢复；备份/标记都不存在时返回 skipped。
func handleScriptRestore(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, msg models.InMessage) (map[string]any, string, string) {
	taskID := strings.TrimSpace(msg.ExecID)
	if !spcTaskIDRe.MatchString(taskID) {
		return nil, "invalid_task_id", "缺少合法的任务 ID，无法定位脚本备份"
	}
	state := spcTaskStore.get(taskID)

	codes := append([]string(nil), msg.ScriptCodes...)
	if len(codes) == 0 && state != nil {
		codes = append(codes, state.Slots...)
	}
	for _, code := range codes {
		if !scriptNameRe.MatchString(code) {
			return nil, "invalid_script", fmt.Sprintf("非法的脚本文件名：%s", code)
		}
	}

	plan := spcRestorePlan{TaskID: taskID, Slots: codes}
	if state != nil {
		plan.Targets = state.writtenTargets()
		plan.LocalIPs = state.LocalIPs
		plan.Dirs = state.dirs()
	}
	if len(plan.Targets) == 0 {
		var hosts []logCollectHostOption
		if value := strings.TrimSpace(msg.IPs); value == "" || strings.EqualFold(value, "all") {
			hosts = spcNodeList(s, ctx)
		}
		targets, _, err := spcTargetSet(msg.IPs, hosts)
		if err != nil {
			return nil, "script_hosts_unknown", err.Error()
		}
		plan.Targets = targets
		plan.LocalIPs = spcLocalAddresses(s, ctx)
	}

	results := restoreScriptHosts(b, ctx, s, plan)
	restored, failed := spcCountRestore(results)
	status := "success"
	if failed > 0 {
		status = "failed"
	}
	if state != nil {
		state.finishRestore(status, "", results)
	}
	logData := map[string]any{
		"task_id":               taskID,
		"script_codes":          codes,
		"script_restore_status": status,
		"script_hosts":          spcPersistHosts(results),
		"restored_count":        restored,
		"failed_count":          failed,
	}
	if failed > 0 {
		return logData, "script_restore_failed", fmt.Sprintf("%d 台节点未能恢复原脚本，需人工核对节点上的备份文件", failed)
	}
	return logData, "", ""
}

// scriptUsedCodes 是日志字段助手：nil 状态返回 nil，避免日志里出现 null 与 [] 两种空值。
func scriptUsedCodes(st *spcTaskState) []string {
	if st == nil {
		return nil
	}
	return st.Slots
}
