package logcollect

// 前置兜底：内置插件落盘校验 + 失败现场快照。
//
// 第一性原理：日志收集能不能跑起来，只取决于两件事——插件文件在不在、
// 采集结果能不能回来。前者过去靠事后探测（WARN）和现场 ls 发现，后者靠
// progress 猜。这两件事都发生在客户侧，平台若不在失败时主动取证，
// 就只能让客户登录设备敲命令（Q2026112300134 的真实代价）。
//
//   - 插件校验放在 create 之前：缺插件时采集必然失败，继续下发只会把
//     「安装不完整」包装成 LC-SPC-003 之类的下游错误，把排查引向权限/磁盘。
//   - 失败快照放在失败之后：任务状态已经写了失败，但「失败时节点长什么样」
//     只有节点知道；把文件存在性、大小、磁盘余量一次性带回来，
//     下次不需要再问现场要 ls/df。

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"terminal_bridge/internal/shared/cluster"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/sshx"
	"time"
)

const (
	// 三个路径就是 log_collect 插件的执行面：plugin 是入口；manifest.json
	// 决定能否加载容器（缺了就是 running without container）；tools 下的
	// progress.json 是采集进度的读取源（缺了 monitor 每轮都报 No such file）。
	logCollectPluginBinary   = "/sf/data/local/acli/plugins/log_collect/plugin"
	logCollectPluginManifest = "/sf/data/local/acli/plugins/log_collect/manifest.json"
	logCollectPluginProgress = "/sf/data/local/acli/plugins/log_collect/tools/lmt_collect_info/progress.json"
)

// buildPluginProbeCommand 只读探测：文件存在且非空才算完整。
// 用 [ -s ] 而不是 [ -f ]：残留的空目录/空文件同样无法执行，必须判缺失。
func buildPluginProbeCommand() string {
	var builder strings.Builder
	for _, path := range []string{logCollectPluginBinary, logCollectPluginManifest, logCollectPluginProgress} {
		quoted := remote.ShellQuote(path)
		builder.WriteString("if [ -s " + quoted + " ]; then printf 'OK %s\\n' " + quoted +
			"; else printf 'MISSING %s\\n' " + quoted + "; fi; ")
	}
	builder.WriteString("true")
	return builder.String()
}

// parsePluginProbe 只认 MISSING 行：其它输出一律忽略，避免把 shell 报错当路径。
func parsePluginProbe(output string) []string {
	missing := make([]string, 0, 3)
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "MISSING ") {
			missing = append(missing, strings.TrimSpace(strings.TrimPrefix(line, "MISSING ")))
		}
	}
	return missing
}

// preflightLogCollectPlugin 逐台检查 log_collect 插件是否完整。
// 返回 (缺失项 "ip:path", 是否真的枚举到了节点)。
func preflightLogCollectPlugin(ctx context.Context, s *sshx.SSHSession) ([]string, bool) {
	command := buildPluginProbeCommand()
	missing := make([]string, 0, 6)
	// 发起节点必查：acli 命令本身在这里执行，插件坏了连 create 都会失败。
	if output, _, _, err := cluster.RunOnHostStreams(s, ctx, "", true, command); err == nil {
		for _, path := range parsePluginProbe(output) {
			missing = append(missing, "local:"+path)
		}
	}
	hosts := cluster.NodeList(s, ctx)
	if len(hosts) == 0 {
		sort.Strings(missing)
		return missing, false
	}
	localIPs := cluster.LocalAddresses(s, ctx)
	targets := make([]string, 0, len(hosts))
	for _, host := range hosts {
		targets = append(targets, host.IP)
	}
	type probeResult struct {
		paths []string
		err   error
	}
	fanout := reconcile.Fanout{Concurrency: cluster.FanoutConcurrency, HostTimeout: -1, BatchMode: true}
	results := fanout.Each(ctx, targets, func(ctx context.Context, ip string) reconcile.HostResult {
		output, _, _, err := cluster.RunOnHostStreams(s, ctx, ip, localIPs[ip], command)
		if err != nil {
			// SSH 不可达不属于「插件不完整」：它有自己的 LC-ENV 码，
			// 这里只记录，由调用方决定是否阻断。
			return reconcile.HostResult{Host: ip, Value: probeResult{err: err}}
		}
		items := make([]string, 0, 3)
		for _, path := range parsePluginProbe(output) {
			items = append(items, ip+":"+path)
		}
		return reconcile.HostResult{Host: ip, Value: probeResult{paths: items}}
	})
	unreachable := 0
	for _, result := range results {
		value, ok := result.Value.(probeResult)
		if !ok {
			unreachable++
			continue
		}
		if value.err != nil {
			unreachable++
			continue
		}
		missing = append(missing, value.paths...)
	}
	sort.Strings(missing)
	evidence.Emit("INFO", "log_collect.plugin_preflight", "发起采集前校验内置插件", "", "", "", "", map[string]any{
		"hosts": len(targets), "missing": missing, "unreachable": unreachable,
	})
	return missing, true
}

// pluginFailureDetail 把缺失项压成一句可读文案（最多 6 条，其余折叠）。
func pluginFailureDetail(missing []string) string {
	const limit = 6
	shown := missing
	if len(shown) > limit {
		shown = shown[:limit]
	}
	detail := strings.Join(shown, ", ")
	if len(missing) > limit {
		detail += fmt.Sprintf(" 等 %d 项", len(missing))
	}
	return detail
}

// buildFailureSnapshotCommand 一次性带回关键路径事实与磁盘余量。
// 全部是只读命令，且带上 2>/dev/null：快照本身绝不能再制造第二类失败。
func buildFailureSnapshotCommand() string {
	paths := []string{logCollectPluginBinary, logCollectPluginManifest, logCollectPluginProgress}
	var builder strings.Builder
	for _, path := range paths {
		quoted := remote.ShellQuote(path)
		builder.WriteString("if [ -s " + quoted + " ]; then printf 'FILE %s %s\\n' " + quoted +
			" \"$(wc -c < " + quoted + " 2>/dev/null | tr -d ' ')\"; " +
			"elif [ -e " + quoted + " ]; then printf 'EMPTY %s\\n' " + quoted + "; " +
			"else printf 'MISSING %s\\n' " + quoted + "; fi; ")
	}
	builder.WriteString("printf 'DISK %s\\n' \"$(df -P /sf 2>/dev/null | tail -n 1)\"; true")
	return builder.String()
}

// parseFailureSnapshot 把脚本输出解析成 files/disk 两个结构化字段。
func parseFailureSnapshot(output string) ([]map[string]any, map[string]any) {
	files := make([]map[string]any, 0, 3)
	disk := map[string]any{}
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		switch {
		case strings.HasPrefix(line, "FILE "):
			rest := strings.TrimPrefix(line, "FILE ")
			path, sizeText, _ := strings.Cut(rest, " ")
			size := 0
			_, _ = fmt.Sscan(sizeText, &size)
			files = append(files, map[string]any{"path": path, "exists": true, "empty": false, "size": size})
		case strings.HasPrefix(line, "EMPTY "):
			files = append(files, map[string]any{"path": strings.TrimPrefix(line, "EMPTY "), "exists": true, "empty": true, "size": 0})
		case strings.HasPrefix(line, "MISSING "):
			files = append(files, map[string]any{"path": strings.TrimPrefix(line, "MISSING "), "exists": false, "empty": false, "size": 0})
		case strings.HasPrefix(line, "DISK "):
			disk["raw"] = strings.TrimSpace(strings.TrimPrefix(line, "DISK "))
		}
	}
	return files, disk
}

// reportFailureSnapshot 在任务失败终态上报节点现场。
// 所有失败只留事件，绝不影响主流程；没有证据票据时退化为事件里的原文。
func reportFailureSnapshot(ctx context.Context, s *sshx.SSHSession, cfg EvidenceConfig, reason string) {
	if s == nil {
		return
	}
	output, stderr, _, err := cluster.RunOnHostStreams(s, ctx, "", true, buildFailureSnapshotCommand())
	if err != nil {
		evidence.Emit("WARN", "log_collect.failure_snapshot_failed", "失败现场快照采集失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "reason": reason, "error": errString(err), "stderr": sshx.TruncateString(stderr, 200),
		})
		return
	}
	files, disk := parseFailureSnapshot(output)
	payload := map[string]any{
		"reason": reason, "host": cfg.NodeIP, "files": files, "disk": disk,
		"collected_at": time.Now().UTC().Format(time.RFC3339),
	}
	if cfg.TaskID == "" || cfg.Ticket == "" || cfg.PlatformURL == "" {
		evidence.Emit("INFO", "log_collect.failure_snapshot_local", "失败现场快照（未配置证据通道，仅落桥日志）", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "reason": reason, "files": files, "disk": disk,
		})
		return
	}
	postCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if _, postErr := cfg.PostEvidence(postCtx, cfg.TaskPath("failure-snapshot"), payload, ""); postErr != nil {
		evidence.Emit("WARN", "log_collect.failure_snapshot_post_failed", "失败现场快照上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "reason": reason, "error": postErr.Error(), "files": files, "disk": disk,
		})
	}
}
