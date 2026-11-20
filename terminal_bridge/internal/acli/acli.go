package acli

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/terminal"
	"time"

	"golang.org/x/net/websocket"

	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/shared/remote"
)

// ── 消息结构 ─────────────────────────────────────────────────────────────────

// models.SpcFile 是平台签发的一份槽位清单原文；摘要由桥独立复算，不信任调用方。
const (
	defaultAcliDownloadURLX86 = "http://acli.sangfor.com.cn:1110/api/public/download/4jotgyfk/latest?architecture=x86_64&api_key=QRlZXabZ8sOrFKtgs1VIfkHJZYQ3QNrcETXgsJaiX9Svz1pjFj"
	defaultAcliDownloadURLARM = "http://acli.sangfor.com.cn:1110/api/public/download/4jotgyfk/latest?architecture=aarch64&api_key=QRlZXabZ8sOrFKtgs1VIfkHJZYQ3QNrcETXgsJaiX9Svz1pjFj"
	defaultMinDiskMB          = 100
)

// portBindRetryAttempts 端口被短暂占用（如崩溃重启后的 TIME_WAIT）时的 bind 重试次数；
// 配合 listenWithRetry 的指数退避抹平时序抖动，避免可恢复的瞬时占用直接判为失败。
func ParseAcliVersion(output string) (version string, buildTime string) {
	info := remote.ParseVersionOutput(output)
	return info.Version, info.BuildTime
}

func probeAcliLatest(ctx context.Context, targetArch, customURL string) (filename string, buildTime string, version string, downloadURL string, err error) {
	downloadURL = customURL
	if downloadURL == "" {
		if targetArch == "aarch64" {
			downloadURL = defaultAcliDownloadURLARM
		} else {
			downloadURL = defaultAcliDownloadURLX86
		}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, downloadURL, nil)
	if err != nil {
		return "", "", "", downloadURL, err
	}
	req.Header.Set("Range", "bytes=0-0")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", "", downloadURL, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
		return "", "", "", downloadURL, fmt.Errorf("下载源返回 HTTP %d", resp.StatusCode)
	}

	cd := resp.Header.Get("Content-Disposition")
	if idx := strings.Index(cd, "filename="); idx != -1 {
		val := strings.Trim(strings.TrimSpace(cd[idx+len("filename="):]), `"'`)
		filename = val
	}

	version, buildTime = parseAcliRemoteName(filename)
	return filename, buildTime, version, downloadURL, nil
}

func HandleAcliSync(b *app.Bridge, ws *websocket.Conn, msg models.InMessage, cui string) {
	// 安装链路必须可观测：远端排障需要看到架构判定、版本比较、下载、上传与安装结果。
	traceID := strings.TrimSpace(msg.TraceID)
	if traceID == "" {
		traceID = "acli-sync-" + logcollect.FirstNonEmpty(strings.TrimSpace(msg.CaseID), "unknown")
	}
	msg.TraceID = traceID

	// 重试起点：由前端「重试上传 / 重试安装」兜底按钮带入。
	//   all     —— 完整流程（默认）；
	//   upload  —— 复用已校验的内存缓存重传，不重新下载；
	//   install —— 复用节点上已校验的安装包重装，不再传字节。
	// 非法值一律回 all：宁可多做一次完整校验，也不要因一个笔误走了未校验的快捷路径。
	retryStage := strings.ToLower(strings.TrimSpace(msg.RetryStage))
	if retryStage != "upload" && retryStage != "install" {
		retryStage = "all"
	}

	s, key := b.ResolveAcliSyncSession(msg)
	if s == nil {
		models.SendMsg(ws, models.OutMessage{
			Type:    "acli_sync_result",
			CaseID:  msg.CaseID,
			Status:  "error",
			TraceID: traceID,
			Message: "SSH 会话不存在（需先 ssh_connect）", ErrorCode: logcollect.ErrEnvSessionMissing,
		})
		obslog.Emit("ERROR", "acli_sync.session_missing", "acli 安装缺少 SSH 会话", traceID, msg.CaseID, msg.NodeIP, cui, nil)
		return
	}
	obslog.Emit("INFO", "acli_sync.start", "acli 检测与安装开始", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"force": msg.Force, "min_disk_mb": msg.MinDiskMB, "custom_url": msg.AcliURL != "", "key": key,
		"retry_stage": retryStage,
	})

	// 阶段二的期望态必须在阶段一开工**前**取定：票据 TTL 只有 10 分钟，而阶段一
	// 最长要跑 660s，装完再去拉 manifest 会拿着过期票据吃 401。
	// 旧前端不带凭据 → 返回 nil，后面两个成功出口都不进阶段二。
	pluginPlan := prepareAcliPluginStage(b, msg, cui, traceID)

	// 总预算是三段独立预算之和再加余量：下载 120s + 上传 300s + 安装 240s。
	// 历史实现用单一 180s 总超时，导致任何一段变慢都会表现成“另一段超时”，
	// 排障只能猜（Q2026102901049 现场三次失败都精确卡在 92s）。
	ctx, cancel := context.WithTimeout(context.Background(), 720*time.Second)
	defer cancel()

	models.SendMsg(ws, models.OutMessage{
		Type:    "acli_sync_progress",
		CaseID:  msg.CaseID,
		Status:  "checking",
		Message: "正在检测 HCI 硬件架构与当前 acli 状态...",
	})

	// 1. 判定架构（S4a：执行机制换 remote.Run，命令与判定语义逐字不动）
	archRes := remote.Run(ctx, sshx.NewSSHRunner(s), "uname -m")
	archOut, exitCode, err := archRes.Stdout, archRes.ExitCode, archRes.Err
	if err != nil || exitCode != 0 {
		models.SendMsg(ws, models.OutMessage{
			Type:    "acli_sync_result",
			CaseID:  msg.CaseID,
			Status:  "error",
			TraceID: traceID,
			Message: fmt.Sprintf("获取系统架构失败 (code=%d): %v", exitCode, err), ErrorCode: logcollect.ErrEnvArchProbeFailed,
		})
		obslog.Emit("ERROR", "acli_sync.arch_probe_failed", "获取系统架构失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"exit_code": exitCode, "error": logcollect.ErrString(err),
		})
		return
	}

	arch := strings.ToLower(strings.TrimSpace(archOut))
	targetArch := "x86_64"
	if strings.Contains(arch, "aarch64") || strings.Contains(arch, "arm64") {
		targetArch = "aarch64"
	} else if !strings.Contains(arch, "x86_64") && !strings.Contains(arch, "amd64") {
		models.SendMsg(ws, models.OutMessage{
			Type:         "acli_sync_result",
			CaseID:       msg.CaseID,
			Status:       "error",
			Architecture: arch,
			TraceID:      traceID,
			Message:      fmt.Sprintf("暂不支持的硬件架构: %s", arch), ErrorCode: logcollect.ErrEnvArchUnsupported,
		})
		obslog.Emit("ERROR", "acli_sync.arch_unsupported", "不支持的硬件架构", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": arch,
		})
		return
	}

	// 2. 检测当前 acli 安装情况
	// 与日志收集面板的 probe 共用同一命令：版本判定与界面展示必须同源，
	// 否则会出现「面板显示 1.0.1 却每次登录都判定落后并重装」的错位。
	versionCmd := logcollect.BuildAcliVersionCommand()
	verOut := remote.Run(ctx, sshx.NewSSHRunner(s), versionCmd).Stdout
	currentVer, currentBuild := ParseAcliVersion(verOut)
	currentBinary := logcollect.ParseAcliBinary(verOut)

	// 3. 探测最新官方包元数据
	remoteFilename, remoteBuild, remoteVersion, downloadURL, probeErr := probeAcliLatest(ctx, targetArch, msg.AcliURL)
	if probeErr != nil {
		log.Printf("[Bridge] acli 官方源探测失败: %v", probeErr)
	}
	// 命名约定再次变更时解析会落空：HTTP 探测本身成功（probeErr=nil），
	// 失败只发生在文件名解析层。不留痕就会重演 Q2026101401677 的静默失效——
	// 判定层把"解析不出"保守跳过，界面却显示"已是最新"，无人察觉。
	if probeErr == nil && remoteFilename != "" && remoteVersion == "" && remoteBuild == "" {
		obslog.Emit("WARN", "acli_sync.build_parse_failed", "远端包名不符合已知命名约定，无法判序，保守跳过升级（可 force）", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"remote_filename": remoteFilename,
		})
	}

	// 4. 版本判定（S4b：descriptor + reconcile.Compare 驱动，见 acliSyncDecision）
	decisionPlan := acliSyncDecision(currentVer, currentBuild, remoteVersion, remoteBuild, msg.Force, retryStage)
	needInstall, decision := acliDecisionOutcome(decisionPlan, currentVer, msg.Force)
	switch decision {
	case "not_installed":
		obslog.Emit("INFO", "acli_sync.not_installed", "HCI 未安装 acli，需要安装", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": targetArch,
		})
	case "force_install":
		obslog.Emit("INFO", "acli_sync.force_install", "收到 force 标记，强制重新安装 acli", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": targetArch,
		})
	case "retry_requested":
		// 对抗性审查：若这里不显式置位，用户点「重试上传」很可能直接落到
		// 「acli 已是最新」分支——按钮点了却什么都不发生，与“报错不管”等价。
		// 用户的显式重试意图本身就是强制信号。
		obslog.Emit("INFO", "acli_sync.retry_requested", "收到重试请求，强制执行对应阶段", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"retry_stage": retryStage, "current_version": currentVer, "current_build": currentBuild,
		})
	case "version_outdated":
		obslog.Emit("INFO", "acli_sync.version_outdated", "acli 远端包更新，需要升级", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"current_version": currentVer, "remote_version": remoteVersion,
			"current_build": currentBuild, "remote_build": remoteBuild,
			"current_date": buildDate(currentBuild), "remote_date": buildDate(remoteBuild),
		})
	case "downgrade_blocked":
		// 远端更旧（版本更低，或同版本但打包日期更早）：禁止降级。
		// 历史 bug：sameBuildDate 只判"是否相等"，预装的新包会被旧官方包误判"落后"
		// 触发 --install --force 降级重装（Q2026111700614：1.0.3 被降到 1.0.0，
		// 连带清掉插件目录与 core 专项）。这里显式跳过并留痕，降级永不自动发生。
		obslog.Emit("INFO", "acli_sync.downgrade_blocked", "远端包更旧，禁止降级，跳过安装", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"current_version": currentVer, "remote_version": remoteVersion,
			"current_build": currentBuild, "remote_build": remoteBuild,
			"current_date": buildDate(currentBuild), "remote_date": buildDate(remoteBuild),
		})
	case "probe_failed":
		// B6：期望证据缺失（源探测失败/包名解析落空）时不得回"已是最新"。
		// 旧实现只打一行日志就落进 up_to_date 分支，断网被伪装成最新。
		obslog.Emit("WARN", "acli_sync.probe_failed", "期望证据缺失，未做升级判定（可强制更新）", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"probe_error": logcollect.ErrString(probeErr), "remote_filename": remoteFilename,
			"current_version": currentVer,
		})
	}

	obslog.Emit("INFO", "acli_sync.version_check", "acli 版本判定完成", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"architecture": targetArch, "current_version": currentVer, "current_build": currentBuild,
		"current_binary":  currentBinary,
		"remote_filename": remoteFilename, "remote_build": remoteBuild, "remote_version": remoteVersion,
		"need_install": needInstall, "force": msg.Force, "probe_error": logcollect.ErrString(probeErr),
	})

	if !needInstall {
		obslog.Emit("INFO", "acli_sync.up_to_date", "acli 已就绪无需安装", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"version": currentVer, "build": currentBuild, "decision": decision, "degraded": decisionPlan.Degraded,
		})
		upToDateMessage := "acli 工具已就绪且为最新版本"
		if decision == "probe_failed" {
			// B6："已是最新"是假结论：如实告知未做升级判定，避免静默失效。
			if probeErr != nil {
				upToDateMessage = "acli 已安装；官方源探测失败，未做升级判定（可强制更新）"
			} else {
				upToDateMessage = "acli 已安装；远端包名无法解析，未做升级判定（可强制更新）"
			}
		}
		// 阶段一成功（无需装工具）→ 紧接阶段二。工具没更新**不代表**插件已是
		// 平台期望态：admin-ui 可能刚改过规格，差量三态必须每台都重算一遍。
		var pluginReceipt *models.AcliPluginStageReceipt
		if acliPluginStageRequested(msg) {
			models.SendMsg(ws, models.OutMessage{
				Type: "acli_sync_progress", CaseID: msg.CaseID,
				Status: "plugins", TraceID: traceID,
				Message: "acli 工具已就绪，正在按平台期望态收敛插件...",
			})
			pluginReceipt = runAcliPluginStage(b, s, msg, pluginPlan, cui, traceID)
			upToDateMessage += pluginReceipt.SummaryText()
		}
		models.SendMsg(ws, models.OutMessage{
			Type:           "acli_sync_result",
			CaseID:         msg.CaseID,
			Status:         "up_to_date",
			Architecture:   targetArch,
			CurrentVersion: currentVer,
			LatestVersion:  remoteBuild,
			TraceID:        traceID,
			Detail:         fmt.Sprintf("%s (build %s)", currentVer, currentBuild),
			Message:        upToDateMessage,
			PluginSync:     pluginReceipt,
			Degraded:       decisionPlan.Degraded,
		})
		obslog.Emit("INFO", "acli_sync.up_to_date", "acli 已就绪无需安装", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": targetArch, "version": currentVer, "build": currentBuild,
		})
		return
	}

	// 5. 校验 /sf/data/local 可用磁盘空间
	models.SendMsg(ws, models.OutMessage{
		Type:    "acli_sync_progress",
		CaseID:  msg.CaseID,
		Status:  "checking_disk",
		Message: "正在校验 /sf/data/local 目录可用空间...",
	})

	spaceOut, spaceCode, spaceErr := s.RunQuickCommand(ctx, "df -P -m /sf/data/local | tail -1 | awk '{print $4}'")
	minDiskMB := defaultMinDiskMB
	if msg.MinDiskMB > 0 {
		minDiskMB = msg.MinDiskMB
	}
	diskPass, availableMB, diskProbed := acliDiskSpaceDecision(spaceOut, spaceCode, spaceErr, minDiskMB)
	if !diskProbed {
		// B7：df 失败/输出不可解析不得静默跳过空间检查：留痕并降级继续安装，
		// 回执如实标注"空间未验证"。旧实现 availableMB=0 让检查条件短路，无人察觉。
		obslog.Emit("WARN", "acli_sync.disk_probe_failed", "磁盘空间探测失败，降级继续安装（可用空间未验证）", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"exit_code": spaceCode, "error": logcollect.ErrString(spaceErr), "output_preview": terminal.FormatOutputPreview(spaceOut, 256, 256),
		})
	}

	if !diskPass {
		log.Printf("[Bridge] /sf/data/local 空间不足: 当前 %d MB, 要求 %d MB", availableMB, minDiskMB)
		models.SendMsg(ws, models.OutMessage{
			Type:   "acli_sync_result",
			CaseID: msg.CaseID,
			Status: "disk_space_insufficient", ErrorCode: logcollect.ErrEnvDiskLow,
			Architecture: targetArch,
			AvailableMB:  availableMB,
			RequiredMB:   int64(minDiskMB),
			TraceID:      traceID,
			Message:      fmt.Sprintf("/sf/data/local 目录可用空间不足（当前: %d MB, 最低需要: %d MB），请清理空间后重试", availableMB, minDiskMB),
		})
		obslog.Emit("ERROR", "acli_sync.disk_insufficient", "acli 安装前磁盘空间不足", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"available_mb": availableMB, "required_mb": minDiskMB, "key": key,
		})
		return
	}

	// 6. 传输互斥：同一会话同一时刻只允许一个安装包在上传。
	//
	// 并发的两个上传会各自写 tmp 再 mv 到同一个 destPath，谁最后落盘完全取决于时序——
	// 这正是“上传到 HCI 的包不对”的可行成因之一。宁可显式拒绝（可重试），
	// 也不要让两次传输互相覆盖。
	release, acquired := acquireAcliTransfer(b, key)
	if !acquired {
		models.SendMsg(ws, models.OutMessage{
			Type: "acli_sync_result", CaseID: msg.CaseID, Status: "error", TraceID: traceID,
			Architecture: targetArch, ErrorCode: logcollect.ErrInstallBusy, Stage: "upload",
			Message: "上一次 acli 安装包传输仍在进行中，请等其结束后再重试（重复上传会互相覆盖同名文件）",
		})
		obslog.Emit("WARN", "acli_sync.busy", "acli 安装包传输进行中，拒绝并发", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"key": key,
		})
		return
	}
	defer release()

	// 7. 落点：文件名来自远端响应头，必须收敛后才能拼进路径与 shell 命令。
	pkgName := sanitizeAcliFilename(remoteFilename, targetArch)
	destPath := "/sf/data/local/" + pkgName

	// 重试安装的兜底：官网不可达时拿不到远端 filename，路径会拼错。
	// 此时优先使用节点上真实存在的包——用户要的是“重试安装”，不是“重新下载”。
	if retryStage == "install" && strings.TrimSpace(remoteFilename) == "" {
		if found := findRemoteAcliPackage(ctx, s); found != "" {
			destPath = found
			pkgName = found[strings.LastIndex(found, "/")+1:]
			obslog.Emit("WARN", "acli_sync.remote_package_fallback", "官网文件名不可得，改用节点上已有的安装包", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"dest_path": destPath, "probe_error": logcollect.ErrString(probeErr),
			})
		}
	}

	obslog.Emit("INFO", "acli_sync.retry_stage", "acli 安装重试起点已确定", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"retry_stage": retryStage, "dest_path": destPath, "package_filename": pkgName,
	})

	cache := packageCache(b)

	// 8. 「只重跑安装 / 只重传」：先验证节点上那份包与本地基线是否一致。
	// 不一致就不复用——上一轮正是因为“盘上那份包不对”才失败的，
	// 无条件复用等于把同一个错误再执行一遍。
	remoteReady := false
	var remoteSize int64
	var remoteAlgo, remoteDigest string
	if retryStage != "all" {
		size, algo, digest, missing, probeErr := probeRemoteAcliPackage(ctx, s, destPath)
		switch {
		case probeErr != nil:
			obslog.Emit("WARN", "acli_sync.reuse_probe_failed", "探测节点已有安装包失败，降级为完整传输", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"dest_path": destPath, "error": logcollect.ErrString(probeErr),
			})
		case missing:
			obslog.Emit("INFO", "acli_sync.reuse_missing", "节点上没有可用安装包，降级为完整传输", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"dest_path": destPath,
			})
		default:
			if reuseErr := verifyReusedRemotePackage(cache.get(key), size, algo, digest); reuseErr == nil {
				remoteReady = true
				remoteSize, remoteAlgo, remoteDigest = size, algo, digest
				obslog.Emit("INFO", "acli_sync.reuse_verified", "节点已有安装包与本地基线一致，复用并跳过传输", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"dest_path": destPath, "remote_size": size, "remote_algo": algo, "remote_digest": digest,
				})
			} else {
				obslog.Emit("WARN", "acli_sync.reuse_rejected", "节点上的安装包不可复用，降级为完整传输", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"dest_path": destPath, "reason": logcollect.ErrString(reuseErr),
					"remote_size": size, "remote_algo": algo, "remote_digest": digest,
				})
			}
		}
	}

	// 9. 传输：下载 → 上传 → 落盘校验，失败自动重试。
	// 重试整体重跑“下载+上传”：因为校验不符既可能是上传截断，也可能是下载侧被污染，
	// 仅重复上传同一份可疑字节无法排除后者。
	var (
		pkg         *acliPackage
		writeRes    acliWriteResult
		integrity   string
		transferMS  int64
		attempts    int
		transferErr error
	)
	transferStart := time.Now()

	if !remoteReady {
		for attempt := 1; attempt <= acliMaxAttempts; attempt++ {
			attempts = attempt

			// 9.1 拿包：优先复用“只重传”场景下的已校验缓存。
			if pkg == nil {
				// 基线包（Data 为空，来自账本恢复）只能做校验基线，不能当上传字节。
				if cached := cache.get(key); cached != nil && cached.Data != nil && retryStage == "upload" {
					pkg = cached
					obslog.Emit("INFO", "acli_sync.package_reused", "复用已校验的安装包缓存，跳过下载", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
						"size": cached.Size, "sha256": cached.SHA256, "md5": cached.MD5, "filename": cached.Filename,
					})
				} else {
					models.SendMsg(ws, models.OutMessage{
						Type: "acli_sync_progress", CaseID: msg.CaseID, Status: "downloading",
						Architecture: targetArch, TraceID: traceID, Attempts: attempt,
						Message: fmt.Sprintf("正在从官方源下载 acli 安装包 (%s，第 %d/%d 次)...", targetArch, attempt, acliMaxAttempts),
					})
					downloadCtx, cancelDownload := context.WithTimeout(context.Background(), acliDownloadTimeout)
					downloadStart := time.Now()
					pkg, transferErr = downloadAcliPackage(downloadCtx, downloadURL, remoteFilename, remoteBuild)
					cancelDownload()
					if transferErr != nil {
						obslog.Emit("ERROR", "acli_sync.download_failed", "下载 acli 安装包失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
							"attempt": attempt, "architecture": targetArch, "download_url": downloadURL, "error": logcollect.ErrString(transferErr),
						})
					} else {
						cache.put(key, pkg)
						// 摘要必须落日志：事后核对“当时到底下到了什么”只能靠这一行。
						obslog.Emit("INFO", "acli_sync.download_verified", "acli 安装包下载完成并通过长度校验", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
							"attempt": attempt, "architecture": targetArch, "filename": pkg.Filename,
							"size": pkg.Size, "declared_length": pkg.DeclaredLen,
							"sha256": pkg.SHA256, "md5": pkg.MD5, "duration_ms": time.Since(downloadStart).Milliseconds(),
						})
					}
				}
			}

			if transferErr == nil {
				// 9.2 上传：预算独立于下载，避免“链路慢”被读成“下载超时”。
				models.SendMsg(ws, models.OutMessage{
					Type: "acli_sync_progress", CaseID: msg.CaseID, Status: "uploading",
					Architecture: targetArch, TraceID: traceID, Attempts: attempt,
					PackageSize: pkg.Size, PackageSHA256: pkg.SHA256, PackagePath: destPath,
					Message: fmt.Sprintf("正在上传安装包至 %s（%d 字节，第 %d/%d 次）...", destPath, pkg.Size, attempt, acliMaxAttempts),
				})
				uploadCtx, cancelUpload := context.WithTimeout(context.Background(), acliUploadTimeout)
				progressFn := func(sent, total int64) {
					percent := int64(0)
					if total > 0 {
						percent = sent * 100 / total
					}
					models.SendMsg(ws, models.OutMessage{
						Type: "acli_sync_progress", CaseID: msg.CaseID, Status: "uploading",
						Architecture: targetArch, TraceID: traceID, Attempts: attempt,
						PackageSize: total, UploadedBytes: sent, Percent: int(percent),
						Message: fmt.Sprintf("正在上传安装包：%d/%d 字节（%d%%）", sent, total, percent),
					})
				}
				writeRes, transferErr = uploadAcliPackage(uploadCtx, s, pkg, destPath, progressFn)
				cancelUpload()
				if transferErr == nil {
					integrity, transferErr = verifyAcliRemoteWrite(pkg, writeRes)
				}
			}

			if transferErr == nil {
				break
			}

			// 失败：丢弃缓存。下一轮重新下载，排除“下载侧被污染”这一可能。
			cache.drop(key)
			pkg = nil
			obslog.Emit("ERROR", "acli_sync.transfer_failed", "acli 安装包传输/校验失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"attempt": attempt, "dest_path": destPath, "error": logcollect.ErrString(transferErr),
				"remote_size": writeRes.RemoteSize, "remote_algo": writeRes.Algo, "remote_digest": writeRes.Digest,
				"remote_fail_reason": writeRes.FailReason,
			})
			if attempt < acliMaxAttempts {
				backoff := time.Duration(attempt) * 2 * time.Second
				models.SendMsg(ws, models.OutMessage{
					Type: "acli_sync_progress", CaseID: msg.CaseID, Status: "retrying",
					Architecture: targetArch, TraceID: traceID, Attempts: attempt,
					Message: fmt.Sprintf("安装包传输失败（%s），%s 后自动重试...", logcollect.ErrString(transferErr), backoff),
				})
				select {
				case <-time.After(backoff):
				case <-ctx.Done():
				}
			}
		}
		transferMS = time.Since(transferStart).Milliseconds()
	}

	if transferErr != nil {
		code, detail, retryable := acliSyncErrorCode(transferErr, logcollect.ErrInstallUpload)
		stage := "upload"
		var syncErr *acliSyncError
		if errors.As(transferErr, &syncErr) && syncErr.Stage != "" {
			stage = syncErr.Stage
		}
		title := "acli 安装包传输失败"
		if !retryable {
			title = "acli 安装包校验失败，需人工确认后重试"
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "acli_sync_result", CaseID: msg.CaseID, Status: "error", TraceID: traceID,
			Architecture: targetArch, ErrorCode: code, Stage: stage, Attempts: attempts, RetryStage: retryStage,
			PackageFilename: pkgName, PackagePath: destPath, Percent: 0,
			Message: fmt.Sprintf("%s（已尝试 %d/%d 次）：%s", title, attempts, acliMaxAttempts, detail),
		})
		obslog.Emit("ERROR", "acli_sync.upload_failed", "acli 安装包上传失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"dest_path": destPath, "attempts": attempts, "retry_stage": retryStage,
			"error_code": code, "stage": stage, "error": detail,
		})
		return
	}

	if remoteReady {
		integrity = remoteAlgo
		obslog.Emit("INFO", "acli_sync.upload_skipped", "复用节点上已校验的安装包，跳过上传", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"dest_path": destPath, "remote_size": remoteSize, "remote_algo": remoteAlgo, "remote_digest": remoteDigest,
		})
	} else {
		obslog.Emit("INFO", "acli_sync.upload_verified", "acli 安装包已上传并通过落盘校验", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"dest_path": destPath, "architecture": targetArch, "attempts": attempts, "duration_ms": transferMS,
			"package_size": pkgSize(pkg), "sha256": pkgSHA(pkg), "md5": pkgMD(pkg),
			"remote_size": writeRes.RemoteSize, "remote_algo": writeRes.Algo, "remote_digest": writeRes.Digest,
			"integrity_state": integrity,
		})
	}

	// 安装包完整性事实：复用节点已有包时本地没有 pkg（可能是缓存过期），
	// 此时把远端探测到的摘要回填，保证 UI 与日志里「哪个包被装上」始终有据可查。
	reportedSize := pkgSize(pkg)
	reportedSHA := pkgSHA(pkg)
	reportedMD5 := pkgMD(pkg)
	if remoteReady {
		if reportedSize == 0 {
			reportedSize = remoteSize
		}
		if reportedSHA == "" && remoteAlgo == "sha256" {
			reportedSHA = remoteDigest
		}
		if reportedMD5 == "" && remoteAlgo == "md5" {
			reportedMD5 = remoteDigest
		}
	}

	// 10. 执行集群安装。
	// 安装与传输使用独立预算：安装器要在集群内分发 20~30MB，用上传预算衡量它
	// 只会在慢集群上制造假失败。
	models.SendMsg(ws, models.OutMessage{
		Type: "acli_sync_progress", CaseID: msg.CaseID, Status: "installing",
		Architecture: targetArch, TraceID: traceID, Attempts: attempts, RetryStage: "install",
		PackageSize: reportedSize, PackageSHA256: reportedSHA, PackageMD5: reportedMD5,
		PackageFilename: pkgName, PackagePath: destPath, IntegrityState: integrity, Percent: 100,
		Message: fmt.Sprintf("安装包已就绪并通过校验（%s），正在执行 acli 集群分发安装（预算 %s）...",
			integrity, acliInstallTimeout),
	})

	installCtx, cancelInstall := context.WithTimeout(ctx, acliInstallTimeout)
	outcome := installAcliPackage(installCtx, s, destPath)
	installTimedOut := installCtx.Err() != nil
	cancelInstall()

	preview, stdoutBytes, stderrBytes := acliOutputForLog(outcome)
	// 结论统一用 InstallSuccess 表达（退出码 0 在 omitempty 下会被吞掉）。
	installOK := outcome.Err == nil && outcome.ExitCode == 0
	// 完整输出先以 progress 形式回传：错误分支也要回传，否则「装不上」时
	// 远端只剩一句『安装失败』，排障又要登录节点。
	models.SendMsg(ws, models.OutMessage{
		Type: "acli_sync_progress", CaseID: msg.CaseID, Status: "install_output",
		Architecture: targetArch, TraceID: traceID, Attempts: attempts, RetryStage: "install",
		PackageFilename: pkgName, PackagePath: destPath, IntegrityState: integrity,
		InstallSuccess: installOK, InstallExitCode: outcome.ExitCode,
		InstallMS: outcome.DurationMS, InstallOutput: preview, InstallStderr: sshx.TruncateString(outcome.Stderr, 2048),
		Message: fmt.Sprintf("acli 安装命令已结束：exit=%d，耗时 %dms（stdout %dB / stderr %dB）",
			outcome.ExitCode, outcome.DurationMS, stdoutBytes, stderrBytes),
	})

	if !installOK {
		code := logcollect.ErrInstallExec
		detail := fmt.Sprintf("acli 安装命令退出码 %d", outcome.ExitCode)
		if installTimedOut {
			code = logcollect.ErrInstallExec
			detail = fmt.Sprintf("acli 安装命令超时（预算 %s）", acliInstallTimeout)
		} else if outcome.Err != nil && outcome.ExitCode == 0 {
			// 无退出码但有错误：通常意味着 SSH 通道在安装过程中断开。
			detail = "acli 安装命令异常中断（SSH 通道中断或远端 shell 退出）"
		}
		obslog.Emit("ERROR", "acli_sync.install_failed", "acli 安装命令执行失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"dest_path": destPath, "command": outcome.Command, "exit_code": outcome.ExitCode,
			"duration_ms": outcome.DurationMS, "timed_out": installTimedOut,
			"stdout_bytes": stdoutBytes, "stderr_bytes": stderrBytes,
			"error": logcollect.ErrString(outcome.Err), "output_preview": preview,
			"package_size": reportedSize, "sha256": reportedSHA, "md5": reportedMD5,
			"integrity_state": integrity, "attempts": attempts,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "acli_sync_result", CaseID: msg.CaseID, Status: "error", TraceID: traceID,
			Architecture: targetArch, ErrorCode: code, Stage: "install", RetryStage: "install",
			Attempts: attempts, PackageSize: reportedSize, PackageSHA256: reportedSHA, PackageMD5: reportedMD5,
			PackageFilename: pkgName, PackagePath: destPath, IntegrityState: integrity,
			InstallExitCode: outcome.ExitCode, InstallMS: outcome.DurationMS,
			InstallOutput: preview, InstallStderr: sshx.TruncateString(outcome.Stderr, 2048),
			Message: fmt.Sprintf("%s。安装包已校验完整，可点击「重试安装」直接复用，无需重新下载。\n%s", detail, preview),
		})
		return
	}

	// 安装器会把 acli 分发到集群各节点，输出里含节点与耗时，完整摘要回传远端供核对。
	obslog.Emit("INFO", "acli_sync.install_done", "acli 集群分发安装完成", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"dest_path": destPath, "command": outcome.Command, "exit_code": outcome.ExitCode,
		"duration_ms": outcome.DurationMS, "stdout_bytes": stdoutBytes, "stderr_bytes": stderrBytes,
		"package_size": reportedSize, "sha256": reportedSHA, "md5": reportedMD5,
		"integrity_state": integrity, "output_preview": preview,
	})

	// 11. 验证安装成果
	verifyCtx, cancelVerify := context.WithTimeout(ctx, acliVerifyTimeout)
	verifyRes := remote.Run(verifyCtx, sshx.NewSSHRunner(s), versionCmd)
	cancelVerify()
	verifyOut, verifyCode := verifyRes.Stdout, verifyRes.ExitCode
	newVer, newBuild := ParseAcliVersion(verifyOut)
	if verifyCode == 0 && newVer != "" {
		log.Printf("[Bridge] acli 安装成功且已验证: ver=%s build=%s key=%s", newVer, newBuild, key)

		// 12. 逐节点校验插件落盘：安装器退出码 0 只证明发起节点装好，
		// 集群分发可能静默留下空目录（Q2026111700614）。缺失只如实警示，
		// 不把本地已成功的安装整体判失败，保留「重试安装」入口。
		pluginCtx, cancelPlugin := context.WithTimeout(ctx, 2*time.Minute)
		pluginStates := verifyClusterPluginLanding(b, pluginCtx, s)
		cancelPlugin()
		installedMessage := "acli 工具安装并集群分发成功！"
		if !diskProbed {
			// B7 降级留痕：空间没验过就是没验过，成功回执里如实标注。
			installedMessage += "\n提示：磁盘空间探测失败，本次安装未验证可用空间。"
		}
		if len(pluginStates) > 0 {
			obslog.Emit("INFO", "acli_sync.node_plugin_verify", "安装后逐节点校验内置插件落盘", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"nodes": pluginStates, "missing": missingPluginNodes(pluginStates),
			})
			if missing := missingPluginNodes(pluginStates); len(missing) > 0 {
				obslog.Emit("WARN", "acli_sync.node_plugin_missing", "部分节点内置插件未落盘", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
					"missing": missing, "nodes": pluginStates,
				})
				installedMessage += fmt.Sprintf("\n警示：节点 %s 的内置插件未落盘（明细见日志），请重试安装。", strings.Join(missing, ", "))
			}
		}

		// 重装会清掉插件目录（含平台写入的 spc 场景清单）：如实观测并提示，
		// 清单将在下次采集 create 时自动重新下发（目录重建已加固）。
		obslog.Emit("WARN", "acli_sync.spc_reset", "acli 重装已重置插件目录，平台场景清单将在下次采集自动重新下发", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"spc_dir": logcollect.SpcDeployDir,
		})
		installedMessage += "\n提示：重装已重置插件目录（含场景清单），平台场景清单将在下次采集自动重新下发。"

		// 阶段一刚装完 acli：重装会清掉插件目录，阶段二正好把平台期望态重新收敛回去
		//（B4 根治：判定只看节点事实，不信"上次装过"）。
		var pluginReceipt *models.AcliPluginStageReceipt
		if acliPluginStageRequested(msg) {
			models.SendMsg(ws, models.OutMessage{
				Type: "acli_sync_progress", CaseID: msg.CaseID,
				Status: "plugins", TraceID: traceID,
				Message: "acli 工具安装完成，正在按平台期望态收敛插件...",
			})
			pluginReceipt = runAcliPluginStage(b, s, msg, pluginPlan, cui, traceID)
			installedMessage += pluginReceipt.SummaryText()
		}

		models.SendMsg(ws, models.OutMessage{
			Type: "acli_sync_result", CaseID: msg.CaseID, Status: "installed",
			Architecture: targetArch, CurrentVersion: newVer, LatestVersion: newBuild,
			TraceID: traceID, Detail: fmt.Sprintf("%s (build %s)", newVer, newBuild),
			Attempts: attempts, RetryStage: retryStage, Stage: "install",
			PackageSize: reportedSize, PackageSHA256: reportedSHA, PackageMD5: reportedMD5,
			PackageFilename: pkgName, PackagePath: destPath, IntegrityState: integrity,
			RemoteSize: writeRes.RemoteSize, RemoteAlgo: writeRes.Algo, RemoteDigest: writeRes.Digest,
			InstallSuccess: true, InstallExitCode: 0, InstallMS: outcome.DurationMS,
			Message:    installedMessage,
			PluginSync: pluginReceipt,
			Degraded:   !diskProbed,
		})
		obslog.Emit("INFO", "acli_sync.installed", "acli 安装并验证成功", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": targetArch, "version": newVer, "build": newBuild, "key": key,
			"package_size": reportedSize, "sha256": reportedSHA, "md5": reportedMD5,
			"integrity_state": integrity, "remote_algo": writeRes.Algo, "remote_digest": writeRes.Digest,
			"install_ms": outcome.DurationMS, "attempts": attempts,
		})
		return
	}

	// 安装命令成功但版本探测失败：包是完整的、装也装完了，问题在可执行入口，
	// 因此给 verify 阶段码并允许「重试安装」（重跑安装会重建 /sf/bin 软链）。
	obslog.Emit("ERROR", "acli_sync.verify_failed", "acli 安装后版本验证失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"exit_code": verifyCode, "output_preview": terminal.FormatOutputPreview(verifyOut, 512, 512),
		"dest_path": destPath, "integrity_state": integrity,
	})
	models.SendMsg(ws, models.OutMessage{
		Type: "acli_sync_result", CaseID: msg.CaseID, Status: "error", TraceID: traceID,
		Architecture: targetArch, ErrorCode: logcollect.ErrInstallVerify, Stage: "verify", RetryStage: "install",
		Attempts: attempts, PackageSize: reportedSize, PackageSHA256: reportedSHA, PackageMD5: reportedMD5,
		PackageFilename: pkgName, PackagePath: destPath, IntegrityState: integrity,
		InstallSuccess: true, InstallExitCode: 0, InstallMS: outcome.DurationMS,
		Message: fmt.Sprintf("acli 安装完成但 acli --version 验证失败（exit=%d）：%s。可点击「重试安装」重新执行安装。",
			verifyCode, terminal.FormatOutputPreview(verifyOut, 512, 512)),
	})
}

// ── 结构化日志与回采（Observability）────────────────────────────────────────
//
// 设计目标（第一性原理 + 业界范式）：
//   1. 统一可观测性：所有日志结构化为 JSON，携带 trace_id / case_id / node_ip /
//      custom_ui 标签，接入平台整体链路（Custom-UI → Bridge → Agent 共享同一 trace）。
//   2. 回采：日志经 WebSocket 以 `bridge_log` 消息实时推给已连接的 Custom-UI 浏览器，
//      由前端统一 POST 到其后台「回采接口」落库（工单关联）。Bridge 为通用代理，
//      不感知后台地址，只按连接 Origin 自动归属 custom_ui。
//   3. 多工单并行/串行：会话本就以 caseID@nodeIP 为键；日志按 case_id 归属与回放。
//   4. 异常重传：环形缓冲保留近期日志，浏览器（重）连接并 ssh_connect 时回放；
//      发送失败暂存 pending，连接恢复后重传；可选落本地文件供进程重启回放。

func buildDate(ts string) string {
	ts = strings.TrimSpace(ts)
	if len(ts) >= 10 {
		return ts[:10]
	}
	return ts
}

// sameBuildDate 判断两个版本时间是否属于同一天。
//
// 为什么只比日期：current_build 是 `acli --version` 的编译时间，remote_build 是安装包
// 文件名里的打包时间，两者不同源；同一版本常常相差 1~2 小时，逐秒比较会永远判定落后，
// 于是每次登录都重下安装包并向全集群分发安装。
//
// 代价（有意接受）：同一天内发布两个包不会自动升级，需要 force 或等跨天后重新登录。
func sameBuildDate(current, remote string) bool {
	cur, rem := strings.TrimSpace(current), strings.TrimSpace(remote)
	if cur == "" || rem == "" {
		return false
	}
	if len(cur) < 10 || len(rem) < 10 {
		return cur == rem
	}
	return cur[:10] == rem[:10]
}

// remoteBuildNewer 判断远端包是否比本地更新（按日期严格大于）。
//
// 为什么必须有序比较而不是只判相等：sameBuildDate 只回答"是否同一天"，
// 无法区分"远端更新"与"远端更旧"。若用 !sameBuildDate 触发安装，预装的
// 新包会被旧官方包误判落后并降级重装（Q2026111700614 现场）。这里只在
// 远端日期严格晚于本地时才升级；同日或本地更新一律跳过，降级永不自动发生。
//
// 保守原则：任一侧为空或日期段不足 10 位（非标准格式）无法判序，返回 false
// 跳过安装——宁可漏一次升级（可 force），也不冒降级风险。YYYY-MM-DD 字典序
// 即时间序，无需逐秒比较（编译时间与打包时间不同源，同日视为同一版本）。
func remoteBuildNewer(current, remote string) bool {
	cur, rem := buildDate(current), buildDate(remote)
	if len(cur) < 10 || len(rem) < 10 {
		return false
	}
	return rem > cur
}

// 官方包命名约定（单一）：acli_<版本>_<架构>_<YYYYMMDDHHMMSS>.acli，
// 如 acli_1.0.3_x86-c86_64_20261029170012.acli。架构段可为合包形态 x86-c86_64：
// 一个包同时兼容 x86_64 与海光 C86_64（两者 uname -m 均报 x86_64），不是笔误。
// 旧格式 acli-installer_<架构>_YYYY-MM-DD_HH-MM-SS.acli
// 已随发布侧改名退役：latest 端点只发最新包，节点历史残包走重试安装兜底（不解析时间戳），
// 自定义源无活体调用方——保留旧格式分支等于维护一份没有对手方的契约（Q2026101401677 审查结论）。
var (
	acliRemoteVersionRe = regexp.MustCompile(`^acli_(\d+\.\d+\.\d+)_`)
	acliRemoteBuildRe   = regexp.MustCompile(`_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\.acli$`)
)

// parseAcliRemoteName 从官方包名解析版本号与打包时间。
// 任一 segment 不符合约定即返回空串，由调用方决定留痕与保守跳过；
// 这里不做兜底猜测——猜出来的时间戳会直接进判序，比解析失败更危险。
func parseAcliRemoteName(filename string) (version string, buildTime string) {
	if m := acliRemoteVersionRe.FindStringSubmatch(filename); len(m) == 2 {
		version = m[1]
	}
	if m := acliRemoteBuildRe.FindStringSubmatch(filename); len(m) == 7 {
		buildTime = fmt.Sprintf("%s-%s-%s %s:%s:%s", m[1], m[2], m[3], m[4], m[5], m[6])
	}
	return version, buildTime
}

// compareAcliVersions 逐段数值比较版本号（a<b 返回 -1，相等 0，a>b 返回 1）。
// 必须数值比较：字符串序会把 1.0.10 判得比 1.0.9 旧。段数不齐补 0（1.0 == 1.0.0）。
// 任一侧含非数字段返回 0（视为同版本，交日期判序兜底），不做部分比较。
func compareAcliVersions(a, b string) int {
	as, bs := strings.Split(strings.TrimSpace(a), "."), strings.Split(strings.TrimSpace(b), ".")
	for i := 0; i < len(as) || i < len(bs); i++ {
		var av, bv int
		if i < len(as) {
			v, err := strconv.Atoi(as[i])
			if err != nil {
				return 0
			}
			av = v
		}
		if i < len(bs) {
			v, err := strconv.Atoi(bs[i])
			if err != nil {
				return 0
			}
			bv = v
		}
		if av != bv {
			if av < bv {
				return -1
			}
			return 1
		}
	}
	return 0
}

// acliDiskSpaceDecision 判定磁盘空间是否放行（B7）。
//
// 三种结论严格分开：
//   - 探测成功且空间足够 → pass=true, probed=true；
//   - 探测成功但空间不足（含 0 MB）→ pass=false，拒绝安装；
//   - 探测失败/输出不可解析 → pass=true, probed=false：降级继续，
//     但调用方必须 WARN 留痕并在回执标注"空间未验证"。
//
// 旧实现的 availableMB>0 条件把"df 失败"与"真的 0 空间"都短路成跳过：
// 前者静默漏检（B7），后者更是把明确的空间不足放了过去。
func acliDiskSpaceDecision(out string, exitCode int, runErr error, minDiskMB int) (pass bool, availableMB int64, probed bool) {
	if exitCode != 0 || runErr != nil {
		return true, 0, false
	}
	mb, err := strconv.ParseInt(strings.TrimSpace(out), 10, 64)
	if err != nil || mb < 0 {
		return true, 0, false
	}
	return mb >= int64(minDiskMB), mb, true
}

// acliInstallDecision 判定是否需要安装/升级，返回 (needInstall, 留痕事件名)。
// 事件名空串表示"无结论性事件"（已最新）；probe_failed 表示期望证据缺失、
// 未做升级判定（B6：不得回"已是最新"）。
//
// S4b：判定本体拆到 acliSyncDecision（descriptor + reconcile.Compare 驱动），
// 这里只做 Plan → 既有事件名的映射，obslog.Emit switch 与旧单测的判定名不变。
//
// 判序优先级（第一性原理：用最直接的权威信号，代理信号只做兜底）：
//  1. 版本号：新包名携带 semver，本地 `acli --version` 同源可比。版本不同直接定序——
//     旧版本用新日期重打包上架也不会触发降级（纯日期判据在同形场景下会再次降级，
//     与 Q2026111700614 事故同形）；
//  2. 打包日期：仅同版本（或任一侧版本解析落空但日期可得）时比日期，沿用
//     remoteBuildNewer/sameBuildDate 的既定语义——编译时间与打包时间不同源，
//     同日视为同一版本，同日重打包不自动升级（可 force）；
//  3. 都解析不出：降级 Skip（降级安全方向），调用方负责 WARN 留痕。
//
// 未安装 / force / 显式重试是强制信号，先于一切判序。
func acliInstallDecision(currentVer, currentBuild, remoteVersion, remoteBuild string, force bool, retryStage string) (bool, string) {
	plan := acliSyncDecision(currentVer, currentBuild, remoteVersion, remoteBuild, force, retryStage)
	return acliDecisionOutcome(plan, currentVer, force)
}

// acliSyncDecision 构造期望态 descriptor + 节点事实，把三态判定委托给
// reconcile.Compare（S4b：判定不再是一串手写 switch，而是骨架驱动）。
//
// acli 无账本：NodeOnly 让节点事实成为唯一真相。remoteVersion/remoteBuild
// 全空 = 期望证据缺失（官方源探测失败或包名解析落空），Probed=false，
// Compare 降级 Skip 并标 Degraded——断网/源站故障不得被伪装成"已是最新"（B6）。
func acliSyncDecision(currentVer, currentBuild, remoteVersion, remoteBuild string, force bool, retryStage string) reconcile.Plan {
	d := reconcile.DesiredState{
		Kind: reconcile.KindAcliBinary,
		Version: reconcile.VersionSpec{
			Mode:     reconcile.VersionOrdered,
			Expected: remoteVersion,
			Build:    remoteBuild,
		},
	}
	fact := reconcile.NodeFact{
		Probed:    remoteVersion != "" || remoteBuild != "",
		Version:   currentVer,
		BuildTime: currentBuild,
	}
	return reconcile.Compare(d, fact, nil, reconcile.CompareOptions{
		NodeOnly: true,
		Force:    force || retryStage != "all",
		CompareVersion: func(f reconcile.NodeFact, want reconcile.VersionSpec) int {
			// 版本优先：版本不同直接定序（旧版本新日期重打包不得降级）。
			if want.Expected != "" {
				if c := compareAcliVersions(f.Version, want.Expected); c != 0 {
					return c
				}
			}
			// 日期兜底：两侧日期可得才判；同日视为同版本；本地更新禁降级。
			if want.Build != "" && f.BuildTime != "" {
				if remoteBuildNewer(f.BuildTime, want.Build) {
					return -1
				}
				if !sameBuildDate(f.BuildTime, want.Build) {
					return 1
				}
			}
			return 0
		},
	})
}

// acliDecisionOutcome 把 Plan 映射回既有 (needInstall, 事件名)。
// 未安装优先于 force 的旧判序在这里保住：Compare 的 Force 先行，
// 但"未安装 + force"的留痕事件仍必须是 not_installed。
func acliDecisionOutcome(plan reconcile.Plan, currentVer string, force bool) (bool, string) {
	switch plan.Action {
	case reconcile.ActionInstall:
		switch plan.Reason {
		case "not_installed":
			return true, "not_installed"
		case "force":
			if currentVer == "" {
				return true, "not_installed"
			}
			if force {
				return true, "force_install"
			}
			return true, "retry_requested"
		default:
			return true, plan.Reason // version_outdated 等
		}
	case reconcile.ActionBlockedDowngrade:
		return false, "downgrade_blocked"
	default: // Skip
		if plan.Reason == "probe_failed_no_desired" {
			return false, "probe_failed"
		}
		return false, ""
	}
}

// NormalizeOutputText 把命令输出规整为合法 UTF-8。
//
// acli 安装器在部分版本按本地代码页输出（GBK），原始字节直接进 JSON 会被编码成 U+FFFD，
// 排障时关键报错变成一串"�"。这里先校验 UTF-8，不合法再按 GB18030（GBK 超集）解码。
