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
	"terminal_bridge/internal/shared/logcontract"
	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/sshx"
	"time"
)

const (
	// 安装期事实：plugin 是入口；manifest.json 决定能否加载容器
	//（缺了就是 running without container）。两者随 acli 安装包落盘，
	// 因此 create 之前不存在即代表安装/集群分发不完整，必须阻断。
	logCollectPluginBinary   = "/sf/data/local/acli/plugins/log_collect/plugin"
	logCollectPluginManifest = "/sf/data/local/acli/plugins/log_collect/manifest.json"

	// 运行时产物：progress.json 不是安装包自带的静态文件，而是节点
	// sfd_main.py 在采集任务启动（create 之后）用 echo 覆盖写入的进度文件，
	// 采集结束时再更新。create 之前不存在是正常状态，绝不能当安装缺陷。
	// 2026-11-24 事故：#1617 把它列入前置阻断，导致全新安装后 3 个现场
	// 4 单全部被 LC-INSTALL-010 误拦（Q2026112300987 等）。该文件只用于
	// 失败现场快照取证，不参与任何阻断判定。
	logCollectRuntimeProgress = "/sf/data/local/acli/plugins/log_collect/tools/lmt_collect_info/progress.json"
)

// logCollectInstallArtifacts 是 create 前唯一允许阻断的安装期清单。
// 新增路径前必须先回答：它是否由 acli 安装器落盘？运行时生成的一律不许进。
func logCollectInstallArtifacts() []string {
	return []string{logCollectPluginBinary, logCollectPluginManifest}
}

// buildPluginProbeCommand 只读探测：安装期文件存在且非空才算完整。
// 用 [ -s ] 而不是 [ -f ]：残留的空目录/空文件同样无法执行，必须判缺失。
func buildPluginProbeCommand() string {
	var builder strings.Builder
	for _, path := range logCollectInstallArtifacts() {
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

// preflightTargetHosts 确定本次 create 前置插件校验的目标节点集合。
//
// 第一性原理：用户明确点名采集特定节点（如 10.124.1.20,10.124.1.22）时，
// 集群里其它未参采节点是否缺插件与本次采集成功与否毫无物理因果。
// 旧实现忽略 requestedIPs 强查全集群，导致 Q2026112600533（14 节点集群仅选 2 台）
// 被无关节点缺失拦截。仅在 all/空 时检查全集群；点名模式只查目标节点（发起节点另行必查）。
func preflightTargetHosts(requestedIPs string, clusterHosts []string) ([]string, string) {
	if selected := expectedHostsFromIPs(requestedIPs); len(selected) > 0 {
		out := append([]string(nil), selected...)
		sort.Strings(out)
		return out, "selected"
	}
	out := append([]string(nil), clusterHosts...)
	sort.Strings(out)
	return out, "all"
}

// preflightLogCollectPlugin 逐台检查 log_collect 插件是否完整。
// 返回 (缺失项 "ip:path", 是否真的枚举到了目标节点)。
func preflightLogCollectPlugin(ctx context.Context, s *sshx.SSHSession, cfg EvidenceConfig, requestedIPs string) ([]string, bool) {
	command := buildPluginProbeCommand()
	missing := make([]string, 0, 6)
	// 发起节点必查：acli 命令本身在这里执行，插件坏了连 create 都会失败。
	if output, _, _, err := cluster.RunOnHostStreams(s, ctx, "", true, command); err == nil {
		for _, path := range parsePluginProbe(output) {
			missing = append(missing, "local:"+path)
		}
	}
	selected := expectedHostsFromIPs(requestedIPs)
	var clusterIPs []string
	if len(selected) == 0 {
		hosts := cluster.NodeList(s, ctx)
		if len(hosts) == 0 {
			sort.Strings(missing)
			return missing, false
		}
		clusterIPs = make([]string, 0, len(hosts))
		for _, host := range hosts {
			clusterIPs = append(clusterIPs, host.IP)
		}
	}
	targets, scope := preflightTargetHosts(requestedIPs, clusterIPs)
	localIPs := cluster.LocalAddresses(s, ctx)
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
	// 事件里显式区分「阻断判据」与「仅观测项」以及预检范围：事后可直接核对
	// 为什么只检选定节点或全集群。
	evidence.Emit("INFO", "log_collect.plugin_preflight", "发起采集前校验内置插件", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
		"hosts":                  len(targets),
		"scope":                  scope,
		"missing":                missing,
		"unreachable":            unreachable,
		"task_id":                cfg.TaskID,
		"blocking_paths":         logCollectInstallArtifacts(),
		"observation_only_paths": []string{logCollectRuntimeProgress},
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
	// 快照是「失败时节点长什么样」的取证，不是判定：运行时进度文件保留在这里，
	// 便于区分「采集从未启动」与「采集启动后进度没落盘」，但绝不参与阻断。
	paths := append(logCollectInstallArtifacts(), logCollectRuntimeProgress)
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

// detectLogCollectCreateOutputError 识别 acli create 输出中的确定性业务拒绝行。
//
// 为什么在 exitCode==0 与 exitCode!=0 两侧都要调它：
//   - acli 在部分前置检查失败时（例如 2026-11-25 Q2026112500790：某节点 /sf/data/local
//     可用空间 5.72GB 小于 6GB）会打印明确拒绝提示但以 exit 0 退出；
//   - 在日期越界（Q2026112500714）或离线 IP（Q2026112301741）时以 exit 1 退出，
//     若不解析就会落成泛化 LC-EXEC-003，掩盖具体处置动作。
//
// 严格忽略 acli 自身打印的结构化 JSON 运行日志行（如 cpuset.cpus / vtp-rpc-ssl.pem），
// 防止正常任务被噪音误杀。
func detectLogCollectCreateOutputError(output string) (string, string, bool) {
	cleaned := stripAnsi(output)
	for _, raw := range strings.Split(strings.ReplaceAll(cleaned, "\r\n", "\n"), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		// acli 的结构化错误日志形如 `2026/11/25 ... {"@timestamp":...}`，不属于业务拒绝结论。
		if strings.HasPrefix(line, "{") || strings.Contains(line, `"@timestamp"`) {
			continue
		}
		lower := strings.ToLower(line)
		switch {
		case (strings.Contains(line, "可用空间") && (strings.Contains(line, "小于") || strings.Contains(line, "不足"))) ||
			strings.Contains(lower, "no space left on device") ||
			strings.Contains(lower, "insufficient disk space"):
			return logcontract.ErrEnvDiskLow, line, true
		case strings.Contains(line, "不在集群在线") || strings.Contains(lower, "not in online"):
			return logcontract.ErrEnvNodeOffline, line, true
		case strings.Contains(line, "指定日期必须在限制的范围"):
			return logcontract.ErrPlugInvalidDate, line, true
		case logCollectModuleMissing(line):
			return logcontract.ErrPlugModuleMissing, line, true
		}
	}
	return "", "", false
}
