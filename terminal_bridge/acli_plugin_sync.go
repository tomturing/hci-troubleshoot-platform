package main

// acli_sync 第二阶段编排：浏览器触发 → 桥持票据拉平台期望态 → 逐插件差量三态收敛。
//
// 失败语义（收口设计 §3.3，逐条对应代码）：
//  1. 阶段一失败 → 本文件所有入口都不被调用（handleAcliSync 只在两个成功出口挂接）；
//  2. 阶段二失败不回滚阶段一：回执只作为 plugin_sync 附加信息，不参与阶段一 status；
//  3. 逐插件、逐节点独立结论：某台失败只影响该台的动作，不波及其余插件；
//  4. 无备份不删等构造性不变量在设备侧脚本里保证（见 acli_plugin_deploy.go）；
//  5. 节点事实优先：判定只看设备侧探测结果，绝不因为"上次装过"就 Skip（B4 根治）。

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"path"
	"strings"
	"sync"
	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/sshx"
	"time"

	"golang.org/x/crypto/ssh"
)

// models.AcliPluginHostOutcome 是一台节点对一个插件的最终事实。

// models.AcliPluginPluginResult 是一个插件跨节点的聚合结论。

// models.AcliPluginStageReceipt 是阶段二回执（WS acli_sync_result.plugin_sync）。

// acliPluginActionRank 用于"取最差动作"：失败必须盖过任何乐观结论。
func acliPluginActionRank(action string) int {
	switch action {
	case acliPluginActionFailed, acliPluginActionUnreachable:
		return 4
	case acliPluginActionOverwrite:
		return 3
	case acliPluginActionInstall:
		return 2
	case acliPluginActionSkip:
		return 1
	default:
		return 0
	}
}

// acliPluginSummaryText 给阶段一成功文案追加一行阶段二结论；前端不解析结构化字段时也能看懂。

// acliPluginStageRequested 判断本次 acli_sync 是否带了阶段二凭据。
//
// 单独拆出来是为了“旧前端一条消息都不多发”：阶段二未启用时连 progress 事件也不发，
// 否则旧界面会收到一个它不认识的中间态。
func acliPluginStageRequested(msg models.InMessage) bool {
	return strings.TrimSpace(msg.PlatformURL) != "" && strings.TrimSpace(msg.PluginTicket) != ""
}

// acliPluginStagePlan 是“期望态已拉到、尚未执行”的中间态。
//
// 为什么必须在阶段一开始**之前**就拉 manifest：票据 TTL 上限 10 分钟，而阶段一的
// 分段预算就有 660s（下载 120 + 上传 300 + 安装 240）。等阶段一装完再去取票，
// 要么拿到一张过期票据 → 阶段二永远 401，“总是触发”退化成“总是失败”。
// 提前拉取还顺带把期望态定格成本次同步的快照：装到一半 admin-ui 改规格，
// 不会让这一次收敛一半旧一半新。
type acliPluginStagePlan struct {
	plugins  []acliPluginDesired
	manifest *acliPluginManifestResponse
	fetchErr error
	started  time.Time
}

// prepareAcliPluginStage 在阶段一开工前用票据换取期望态。返回 nil 表示未启用。
func (b *Bridge) prepareAcliPluginStage(msg models.InMessage, cui, traceID string) *acliPluginStagePlan {
	if !acliPluginStageRequested(msg) {
		// 契约兼容：没有 platform_url / plugin_ticket 就是没启用阶段二，
		// 这不是失败，后续也不会发回执（旧前端不会因为多一个 null 字段出错）。
		return nil
	}
	caseID := strings.TrimSpace(msg.CaseID)
	logpush.Blog("INFO", "acli_plugin_sync.start", "acli 插件第二阶段开工（先拉期望态）", traceID, caseID, msg.NodeIP, cui, map[string]any{
		"platform_url": strings.TrimSpace(msg.PlatformURL),
	})
	plan := &acliPluginStagePlan{started: time.Now()}
	fetchCtx, cancel := context.WithTimeout(context.Background(), acliPluginFetchTimeout)
	defer cancel()
	manifest, err := b.fetchAcliPluginManifest(
		fetchCtx,
		strings.TrimSpace(msg.PlatformURL),
		strings.TrimSpace(msg.PluginTicket),
		caseID,
		msg.TransferTLSInsecure,
	)
	if err != nil {
		// 拉失败不在此上报：阶段一照旧跑完，回执里带原因码——否则一次平台抖动
		// 会把“工具没装”这个更严重的结论掩盖掉。
		plan.fetchErr = err
		logpush.Blog("WARN", "acli_plugin_sync.manifest_failed", "拉取插件期望态失败", traceID, caseID, msg.NodeIP, cui, map[string]any{
			"reason": acliPluginErrorCode(err), "detail": err.Error(),
		})
		return plan
	}
	plan.manifest = manifest
	plan.plugins = manifest.Plugins
	logpush.Blog("INFO", "acli_plugin_sync.manifest_fetched", "已拉取插件期望态", traceID, caseID, msg.NodeIP, cui, map[string]any{
		"desired_count":    len(manifest.Plugins),
		"desired_plugins":  sortedPluginNames(manifest.Plugins),
		"manifest_version": manifest.ManifestVersion,
	})
	return plan
}

// runAcliPluginStage 是 handleAcliSync 两个成功出口的唯一入口。
//
// 上下文必须与阶段一的 720s 总预算**脱钩**：S4a 明确不动阶段一的外壳与预算，
// 插件同步不能反过来把阶段一的预算账改歪。会话被关掉时不必特意中断：
// SSH 通道一断，逐节点操作会立刻以 unreachable 收口，不会进入无限等待。
func (b *Bridge) runAcliPluginStage(
	s *sshx.SSHSession,
	msg models.InMessage,
	plan *acliPluginStagePlan,
	cui, traceID string,
) *models.AcliPluginStageReceipt {
	if plan == nil {
		return nil
	}
	stageCtx, cancel := context.WithTimeout(context.Background(), acliPluginStageBudget)
	defer cancel()
	return b.syncAcliPluginsFromPlatform(stageCtx, s, msg, plan, cui, traceID)
}

// syncAcliPluginsFromPlatform 执行第二阶段（期望态已由 prepare 取定）。
func (b *Bridge) syncAcliPluginsFromPlatform(
	ctx context.Context,
	s *sshx.SSHSession,
	msg models.InMessage,
	plan *acliPluginStagePlan,
	cui, traceID string,
) *models.AcliPluginStageReceipt {
	started := plan.started
	caseID := strings.TrimSpace(msg.CaseID)
	receipt := &models.AcliPluginStageReceipt{Stage: "plugin"}
	if plan.fetchErr != nil {
		receipt.Reason = acliPluginErrorCode(plan.fetchErr)
		receipt.Detail = plan.fetchErr.Error()
		receipt.ElapsedMS = time.Since(started).Milliseconds()
		return receipt
	}
	plugins := plan.plugins
	receipt.ManifestVersion = plan.manifest.ManifestVersion
	receipt.DesiredCount = len(plugins)
	if len(plugins) == 0 {
		receipt.OK = true
		receipt.Warnings = append(receipt.Warnings, "manifest_empty")
		receipt.ElapsedMS = time.Since(started).Milliseconds()
		logpush.Blog("INFO", "acli_plugin_sync.no_desired_plugins", "平台未定义任何插件期望态，阶段二无事可做", traceID, caseID, msg.NodeIP, cui, nil)
		return receipt
	}

	// 目标节点集合：与 spc 扇出同源（`acli platform node list` + 本机地址表）。
	hosts := spcNodeList(s, ctx)
	localIPs := spcLocalAddresses(s, ctx)
	targets := make([]acliPluginTarget, 0, len(hosts))
	for _, host := range hosts {
		ip := strings.TrimSpace(host.IP)
		if ip == "" {
			continue
		}
		targets = append(targets, acliPluginTarget{IP: ip, Local: localIPs[ip]})
	}
	if len(targets) == 0 {
		// 节点清单探测失败时不能"顺手跳过"：至少把发起节点收敛掉，并如实标注
		// 覆盖范围不完整（B6/B8 同族教训：静默降级比失败更难排查）。
		targets = []acliPluginTarget{{IP: sessionHostAddress(s, msg.NodeIP), Local: true}}
		receipt.Warnings = append(receipt.Warnings, "node_list_unavailable_only_local_converged")
		logpush.Blog("WARN", "acli_plugin_sync.node_list_unavailable", "集群节点清单不可得，仅收敛发起节点", traceID, caseID, msg.NodeIP, cui, map[string]any{
			"fallback_ip": targets[0].IP,
		})
	}
	for _, target := range targets {
		receipt.Nodes = append(receipt.Nodes, target.IP)
	}

	// 逐插件串行、插件内逐节点并发：
	//   - 串行让"某插件失败只回滚该插件"这条不变量在编排层也成立；
	//   - 设备侧 flock 已经把同节点并发安装互斥掉了，插件间并发只会加长锁等待。
	for _, desired := range plugins {
		result := b.reconcileAcliPlugin(ctx, s, desired, targets, cui, traceID, caseID)
		receipt.Plugins = append(receipt.Plugins, result)
		for _, outcome := range result.Hosts {
			switch outcome.Action {
			case acliPluginActionInstall:
				receipt.Installed++
			case acliPluginActionOverwrite:
				receipt.Overwritten++
			case acliPluginActionSkip:
				receipt.Skipped++
			default:
				receipt.Failed++
			}
		}
	}
	receipt.OK = receipt.Failed == 0
	receipt.ElapsedMS = time.Since(started).Milliseconds()
	if !receipt.OK {
		receipt.Reason = "plugin_deploy_failed"
	}
	level := "INFO"
	if !receipt.OK {
		level = "WARN"
	}
	logpush.Blog(level, "acli_plugin_sync.done", "acli 插件第二阶段结束", traceID, caseID, msg.NodeIP, cui, map[string]any{
		"ok": receipt.OK, "installed": receipt.Installed, "overwritten": receipt.Overwritten,
		"skipped": receipt.Skipped, "failed": receipt.Failed,
		"nodes": receipt.Nodes, "elapsed_ms": receipt.ElapsedMS, "warnings": receipt.Warnings,
	})
	return receipt
}

// acliPluginTarget 是一次收敛的一个目标节点。
type acliPluginTarget struct {
	IP    string
	Local bool
}

// reconcileAcliPlugin 对单个插件做逐节点差量判定与执行。
func (b *Bridge) reconcileAcliPlugin(
	ctx context.Context,
	s *sshx.SSHSession,
	desired acliPluginDesired,
	targets []acliPluginTarget,
	cui, traceID, caseID string,
) models.AcliPluginPluginResult {
	result := models.AcliPluginPluginResult{
		Name:     desired.Name,
		Version:  desired.Version,
		SpecHash: desired.SpecHash,
		Action:   acliPluginActionSkip,
	}
	outcomes := make([]models.AcliPluginHostOutcome, len(targets))
	sem := make(chan struct{}, spcFanoutConcurrency)
	var wg sync.WaitGroup
	for index, target := range targets {
		wg.Add(1)
		go func(index int, target acliPluginHostStatus) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			outcomes[index] = b.reconcileAcliPluginOnHost(ctx, s, desired, target, cui, traceID, caseID)
			if acliPluginActionRank(outcomes[index].Action) > acliPluginActionRank(result.Action) {
				result.Action = outcomes[index].Action
			}
		}(index, acliPluginHostStatus(target))
	}
	wg.Wait()
	result.Hosts = outcomes
	return result
}

// acliPluginHostStatus 是为了让 goroutine 闭包捕获时的类型意图更明确（等同 acliPluginTarget）。
type acliPluginHostStatus = acliPluginTarget

// reconcileAcliPluginOnHost 在单台节点上完成"探测→判定→（必要时）部署→自检"。
func (b *Bridge) reconcileAcliPluginOnHost(
	ctx context.Context,
	s *sshx.SSHSession,
	desired acliPluginDesired,
	target acliPluginTarget,
	cui, traceID, caseID string,
) models.AcliPluginHostOutcome {
	outcome := models.AcliPluginHostOutcome{IP: target.IP, Action: acliPluginActionSkip}
	started := time.Now()
	probeCtx, cancelProbe := context.WithTimeout(ctx, acliPluginHostProbeTimeout)
	stdout, stderr, exitCode, err := runAcliPluginOnHost(s, probeCtx, target, buildAcliPluginProbeCommand(desired), acliPluginHostProbeTimeout)
	cancelProbe()
	actual := parseAcliPluginProbeOutput(target.IP, stdout, exitCode, err)
	action, reason := compareAcliPluginState(desired, actual)
	outcome.Action = action
	outcome.Reason = reason
	if action == acliPluginActionSkip {
		outcome.ElapsedMS = time.Since(started).Milliseconds()
		return outcome
	}
	if action == acliPluginActionOverwrite && strings.HasPrefix(reason, "probe") {
		// 探测本身不可信：仍然收敛（事实优先），但把降级原因带到回执里。
		logpush.Blog("WARN", "acli_plugin_sync.probe_degraded", "插件事实探测降级，按覆盖安装收敛", traceID, caseID, target.IP, cui, map[string]any{
			"plugin": desired.Name, "reason": reason, "stderr": sshx.TruncateString(stderr, 512),
		})
	}

	// deploy_id 先定下来再渲染：脚本内的临时目录与脚本自身的落盘文件名必须同源，
	// 否则 rollback 找不到自己的备份目录（B1 同族风险）。
	deployID := acliPluginDeployID(desired)
	script, err := renderAcliPluginDeployScript(desired, deployID)
	if err != nil {
		outcome.Action = acliPluginActionFailed
		outcome.Reason = acliPluginErrorCode(err)
		outcome.Error = err.Error()
		return outcome
	}
	scriptPath := acliPluginScriptPath(deployID)
	if writeErr := b.writeAcliPluginScript(ctx, s, target, scriptPath, []byte(script)); writeErr != nil {
		outcome.Action = acliPluginActionFailed
		outcome.Reason = acliPluginErrorCode(writeErr)
		outcome.Error = writeErr.Error()
		outcome.ElapsedMS = time.Since(started).Milliseconds()
		logpush.Blog("ERROR", "acli_plugin_sync.script_write_failed", "部署脚本写入节点失败", traceID, caseID, target.IP, cui, map[string]any{
			"plugin": desired.Name, "path": scriptPath, "reason": outcome.Reason, "detail": outcome.Error,
		})
		return outcome
	}

	command := fmt.Sprintf("bash %s; rc=$?; rm -f %s; exit $rc", shellQuote(scriptPath), shellQuote(scriptPath))
	deployCtx, cancelDeploy := context.WithTimeout(ctx, acliPluginHostDeployTimeout)
	stdout, stderr, exitCode, err = runAcliPluginOnHost(s, deployCtx, target, command, acliPluginHostDeployTimeout)
	cancelDeploy()
	deploy := parseAcliPluginDeployResult(stdout, exitCode)
	outcome.Status = deploy.Status
	outcome.Error = deploy.Error
	outcome.Smoke = sshx.TruncateString(deploy.SmokeOutput, 512)
	outcome.ElapsedMS = time.Since(started).Milliseconds()
	switch {
	case deploy.OK:
		// 设备侧自检通过：动作保持判定结论（新增 / 有修改），回执据此计数。
		logpush.Blog("INFO", "acli_plugin_sync.host_success", "插件在节点上收敛成功", traceID, caseID, target.IP, cui, map[string]any{
			"plugin": desired.Name, "action": action, "version": deploy.Version,
		})
	case err != nil && exitCode == -1:
		outcome.Action = acliPluginActionFailed
		outcome.Reason = "host_channel_error"
		outcome.Error = errString(err)
	case exitCode != 0:
		outcome.Action = acliPluginActionFailed
		outcome.Reason = "host_exec_failed"
		outcome.Error = firstNonEmpty(deploy.Error, fmt.Sprintf("exit=%d stderr=%s", exitCode, sshx.TruncateString(stderr, 512)))
	default:
		// 脚本正常收尾但自检不是 success：可能是 rolled_back，也可能压根没打出结果块。
		outcome.Action = acliPluginActionFailed
		outcome.Reason = "device_selfcheck_not_passed"
		outcome.Error = firstNonEmpty(deploy.Error, "status="+deploy.Status)
	}
	if outcome.Action == acliPluginActionFailed {
		logpush.Blog("WARN", "acli_plugin_sync.host_failed", "插件在节点上收敛失败", traceID, caseID, target.IP, cui, map[string]any{
			"plugin": desired.Name, "reason": outcome.Reason, "error": outcome.Error,
			"device_status": outcome.Status, "exit_code": exitCode,
		})
	}
	return outcome
}

// runAcliPluginOnHost 在目标节点执行一条命令：本机直接用当前会话，其余节点从发起节点跳 ssh。
//
// 与 spc 扇出同一套执行原语（BatchMode + ConnectTimeout=5），但超时必须各自独立：
// 探测 45s、部署 8min，共用 spc 的 30s 会把冒烟阶段截断成假失败。
func runAcliPluginOnHost(s *sshx.SSHSession, ctx context.Context,
	target acliPluginTarget,
	command string,
	timeout time.Duration) (string, string, int, error) {
	full := command
	if !target.Local {
		full = buildSpcSSHCommand(target.IP, command)
	}
	hostCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return s.RunQuickCommandStreams(hostCtx, full)
}

// writeAcliPluginScript 用 stdin 通道把部署脚本落到节点，复用安装包那套
// 「先校验再 mv、以退出码表达结论」的原子写协议（退出码 20-23）。
//
// 为什么不用 printf 分片塞 argv：netdoctor 一类插件的脚本近 1MB，Linux 单参数
// 上限约 128KB，旧实现只能按 48KB 分片下发（每条命令一次 WS/SSH 往返）。
// stdin 通道一次成型，且落盘后仍有摘要复核，不牺牲完整性。
func (b *Bridge) writeAcliPluginScript(
	ctx context.Context,
	s *sshx.SSHSession,
	target acliPluginTarget,
	scriptPath string,
	data []byte,
) error {
	tmpPath := fmt.Sprintf("%s.tmp.%d", scriptPath, time.Now().UnixNano())
	// 两个摘要都要算：节点上既没 sha256sum 只有 md5sum 时，远端写盘命令会退到
	// md5 口径（acli_package.go 的降级链），这里留空 MD5 会把本来就写对了的脚本
	// 当成 digest_mismatch 拒掉。MD5 在此只作传输完整性校验，不作安全凭证。
	md5Sum := md5.Sum(data)
	pkg := &acliPackage{
		Data:   data,
		Size:   int64(len(data)),
		SHA256: sha256Hex(data),
		MD5:    hex.EncodeToString(md5Sum[:]),
	}
	// 目录必须先存在：acli 重装会清掉整个 tmp 目录，cat > 在缺目录时直接失败。
	mkdirCtx, cancelMkdir := context.WithTimeout(ctx, acliPluginHostProbeTimeout)
	_, _, exitCode, err := runAcliPluginOnHost(s, mkdirCtx, target,
		"mkdir -p "+shellQuote(path.Dir(scriptPath)), acliPluginHostProbeTimeout)
	cancelMkdir()
	if err != nil || exitCode != 0 {
		return newAcliPluginError("script_dir_prepare_failed",
			fmt.Sprintf("exit=%d err=%s", exitCode, errString(err)))
	}

	inner := buildAcliRemoteWriteCommand(tmpPath, scriptPath, pkg, "0755")
	command := inner
	if !target.Local {
		command = buildSpcSSHCommand(target.IP, inner)
	}
	writeCtx, cancel := context.WithTimeout(ctx, acliUploadTimeout)
	defer cancel()
	stdout, stderr, exitCode, err := runSSHCommandWithStdin(s, writeCtx, command, data)
	if err != nil && exitCode == -1 {
		return newAcliPluginError("script_write_channel_error", errString(err))
	}
	result := parseAcliWriteOutput(stdout)
	if _, verifyErr := verifyAcliRemoteWrite(pkg, result); verifyErr != nil {
		// 校验失败：清掉残片，别让下一次同步把半份脚本当成已就位。
		cleanupCtx, cancelCleanup := context.WithTimeout(context.Background(), 15*time.Second)
		cleanupAcliTmp(cleanupCtx, s, tmpPath)
		cancelCleanup()
		return newAcliPluginError("script_write_verify_failed",
			fmt.Sprintf("%s; stderr=%s", verifyErr.Error(), sshx.TruncateString(stderr, 512)))
	}
	return nil
}

// runSSHCommandWithStdin 在当前 SSH 连接上执行一条命令并把 data 作为 stdin 送进去。
//
// 单独写一份而不是改 uploadAcliPackage：本函数的命令可能是
// `ssh root@node 'cat > path'`——跳板侧 ssh 会把 stdin 原样转发给远端 cat，
// 这条通道让"写到任意节点"不需要在发起节点上再落一份中间文件。
func runSSHCommandWithStdin(s *sshx.SSHSession, ctx context.Context, command string, data []byte) (string, string, int, error) {
	session, err := s.Client.NewSession()
	if err != nil {
		return "", "", -1, err
	}
	defer session.Close()

	var stdoutBuf, stderrBuf bytes.Buffer
	session.Stdout = &stdoutBuf
	session.Stderr = &stderrBuf
	session.Stdin = bytes.NewReader(data)

	done := make(chan error, 1)
	go func() {
		done <- session.Run(command)
	}()

	select {
	case <-ctx.Done():
		_ = session.Signal(ssh.SIGKILL)
		return stdoutBuf.String(), stderrBuf.String(), -1, ctx.Err()
	case runErr := <-done:
		exitCode := 0
		if runErr != nil {
			var exitErr *ssh.ExitError
			if errors.As(runErr, &exitErr) {
				// 远端命令非零退出是**业务结论**，不是通道故障：把码原样交出去判定。
				exitCode = exitErr.ExitStatus()
			} else {
				exitCode = -1
			}
		}
		return stdoutBuf.String(), stderrBuf.String(), exitCode, runErr
	}
}

// sessionHostAddress 给出发起节点的 IP：节点清单不可得时的兜底收敛目标。
func sessionHostAddress(s *sshx.SSHSession, fallback string) string {
	if trimmed := strings.TrimSpace(fallback); trimmed != "" {
		return trimmed
	}
	if s != nil {
		if host, _, err := net.SplitHostPort(s.Address); err == nil && host != "" {
			return host
		}
		if s.Address != "" {
			return s.Address
		}
	}
	return "unknown"
}
