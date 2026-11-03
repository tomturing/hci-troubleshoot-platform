package main

// 客户机本地中转副本的生命周期管理。
//
// 第一性原理
// ----------
// P2 交付的本地 zip 只有一个用途：作为「HCI → 平台」分片上传的读取源。
// 一旦平台 complete 返回 2xx，这份字节在客户机上就再没有读者，
// 它的属性从"交付必需"变成"纯粹的磁盘占用"。
// 而 P2 的中转盘就是客户机的 %TEMP%（通常正是空间最紧张的 C 盘），
// 所以"用完即删"不是优化项，而是交付链路可持续的必要条件。
//
// 2026-11-03 Q2026110300988：同一台机器当天先后落下 8.6 GB / 2.3 GB / 1.6 GB
// 三个副本，一个都没删；88 MB 的包只写得进 9 MB 就 ENOSPC，
// 而界面上显示的是"拉取中断"。
//
// 对抗性审查
// ----------
//  1. 删得太早 → 用户点"重试"要重拉整包。折中：只删 phase=done 的副本，
//     失败/未完成一律保留（续传与重试都要读它）；
//  2. 删错文件 → 只处理 %TEMP%/hci-log-collect 下的 *.zip，且必须跳过被
//     非终态 job 引用的路径；引用关系取「内存 job 表 ∪ 落盘 json」的并集，
//     因为进程重启后内存表可能还没恢复完，磁盘才是全量事实；
//  3. 删除失败（Windows 上文件被占用）→ 只告警，下一轮再试，绝不阻断交付；
//  4. 老版本遗留的 GB 级副本 → 以"孤儿"身份按保护窗口清理（默认 1 小时），
//     避免误删正在写入但 job 尚未落盘的文件。

import (
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	// deliveryLocalCopyTTL 是"失败终态"副本的保留时长：够用户重试，又不至于堆积。
	deliveryLocalCopyTTL = 72 * time.Hour
	// deliveryLocalCopyMaxBytes 是 hci-log-collect 目录的总量上限。
	//
	// 取 8 GiB：既容得下一个 6 GB 级包 + 一个 1.6 GB 级包（现场常见组合），
	// 又不至于把 16 GB 内存机器的 C 盘吃干。可用环境变量覆盖。
	deliveryLocalCopyMaxBytes int64 = 8 << 30
	// deliveryLocalCopyOrphanGrace 是无引用文件的保护窗口。
	//
	// 为什么需要：拉取刚开始时 job 可能还没有落盘（磁盘满时 save 会失败），
	// 此时文件已经存在但无人引用；给 1 小时缓冲，避免把正在写的副本当垃圾删掉。
	deliveryLocalCopyOrphanGrace = time.Hour
	// deliveryLocalCleanupInterval 是对账周期。
	deliveryLocalCleanupInterval = 6 * time.Hour
)

// localCopyPolicy 是清理策略（全部可由环境变量覆盖，便于现场关闭）。
type localCopyPolicy struct {
	Dir string
	// FailureTTL 是失败终态副本的保留时长；0 表示立即删。
	FailureTTL time.Duration
	// MaxBytes 是目录总量上限；<=0 表示不限制。
	MaxBytes int64
	// DryRun 只统计不删除：老版本升级后首发用它观察一轮真实数据。
	DryRun bool
	// KeepDelivered 为真时成功后也保留副本（仅排障用）。
	KeepDelivered bool
	// Disabled 为真时所有自动清理都不执行。
	Disabled bool
}

// localCopyDirOverride 仅供单测注入临时目录；生产始终为 %TEMP%/hci-log-collect。
var localCopyDirOverride string

// localCopyBaseDir 返回中转副本的根目录。
func localCopyBaseDir() string {
	if localCopyDirOverride != "" {
		return localCopyDirOverride
	}
	return filepath.Join(os.TempDir(), "hci-log-collect")
}

// localCopyPolicyFromEnv 读取策略。
//
// 环境变量：
//   - HCI_BRIDGE_KEEP_LOCAL_DAYS：默认 3；-1 关闭清理；0 表示失败终态也立即删；
//   - HCI_BRIDGE_LOCAL_MAX_MB：目录总量上限（MiB），默认 8192；
//   - HCI_BRIDGE_CLEANUP_DRY_RUN=1：只统计不删除。
func localCopyPolicyFromEnv() localCopyPolicy {
	policy := localCopyPolicy{
		Dir:        localCopyBaseDir(),
		FailureTTL: deliveryLocalCopyTTL,
		MaxBytes:   deliveryLocalCopyMaxBytes,
	}
	if raw := strings.TrimSpace(os.Getenv("HCI_BRIDGE_KEEP_LOCAL_DAYS")); raw != "" {
		if days, err := strconv.Atoi(raw); err == nil {
			switch {
			case days < 0:
				// -1：完全不动客户机文件，用于排障期保留现场。
				policy.Disabled = true
				policy.KeepDelivered = true
			case days == 0:
				policy.FailureTTL = 0
			default:
				policy.FailureTTL = time.Duration(days) * 24 * time.Hour
			}
		}
	}
	if mb := envIntOrDefault("HCI_BRIDGE_LOCAL_MAX_MB", 0); mb > 0 {
		policy.MaxBytes = int64(mb) << 20
	}
	if strings.TrimSpace(os.Getenv("HCI_BRIDGE_CLEANUP_DRY_RUN")) == "1" {
		policy.DryRun = true
	}
	return policy
}

// localCopyCleanupReport 是一轮对账的结果，直接进 delivery.local_cleaned 事件。
type localCopyCleanupReport struct {
	Removed int
	Bytes   int64
	Reasons map[string]int
	DryRun  bool
}

// localCopyRef 是某个本地副本被哪个作业引用的事实。
type localCopyRef struct {
	JobID string
	Phase deliveryPhase
}

// localCopyKey 归一化路径作为引用表的键。
//
// Windows 路径大小写不敏感：同一个文件可能以 C:\Users 与 c:\users 两种形式出现，
// 不归一化就会"引用没匹配上 → 当成孤儿删掉"。
func localCopyKey(path string) string {
	return strings.ToLower(filepath.Clean(strings.TrimSpace(path)))
}

// localCopyReferences 汇总当前所有作业对本地副本的引用。
func (r *deliveryRunner) localCopyReferences() map[string]localCopyRef {
	refs := make(map[string]localCopyRef)
	add := func(job *deliveryJob) {
		if job == nil || strings.TrimSpace(job.LocalPath) == "" {
			return
		}
		refs[localCopyKey(job.LocalPath)] = localCopyRef{JobID: job.JobID, Phase: job.Phase}
	}

	r.mu.Lock()
	for _, job := range r.jobs {
		add(job)
	}
	r.mu.Unlock()

	// 落盘 json 里可能有内存表尚未恢复的作业（启动初期、或内存态已被清）。
	for _, job := range r.store.listAll() {
		add(job)
	}
	return refs
}

// discardDeliveredCopy 在平台确认收到完整包后删除本地中转副本。
//
// 调用点只有一个：advance() 里 complete 成功之后、置 phaseDone 之前。
// 删除失败不改变交付结论（平台已经有包了），只留痕让下一轮对账收拾。
func (r *deliveryRunner) discardDeliveredCopy(job *deliveryJob) {
	if r == nil || job == nil {
		return
	}
	policy := localCopyPolicyFromEnv()
	if policy.Disabled || policy.KeepDelivered {
		return
	}
	path := strings.TrimSpace(job.LocalPath)
	if path == "" {
		return
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		blog("WARN", "delivery.local_clean_failed", "交付成功后删除本地副本失败", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "local_path": path, "reason": "delivered", "error": err.Error(),
		})
		return
	}
	r.mu.Lock()
	job.LocalDeletedAt = time.Now().UTC().Format(time.RFC3339Nano)
	r.mu.Unlock()
	_ = r.store.save(job)
	blog("INFO", "delivery.local_cleaned", "交付成功，已删除本地中转副本", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "local_path": path, "reason": "delivered",
	})
}

// reconcileLocalCopies 清理目录中不再需要的副本。
//
// 三类删除理由互不重叠：
//   - delivered：作业已交付成功，副本无读者；
//   - ttl：失败终态超过保留期；
//   - orphan：没有任何作业引用，且超过保护窗口（老版本遗留的典型形态）。
//
// 非终态作业引用的文件在任何分支下都不会被删——这是本函数唯一的不变量。
func (r *deliveryRunner) reconcileLocalCopies(now time.Time) localCopyCleanupReport {
	report := localCopyCleanupReport{Reasons: make(map[string]int)}
	if r == nil {
		return report
	}
	policy := localCopyPolicyFromEnv()
	report.DryRun = policy.DryRun
	if policy.Disabled {
		return report
	}

	entries, err := os.ReadDir(policy.Dir)
	if err != nil {
		// 目录不存在（从未交付过）是最常见情形，不当异常。
		return report
	}
	refs := r.localCopyReferences()

	type candidate struct {
		path string
		mod  time.Time
		size int64
		ref  localCopyRef
		used bool
	}
	candidates := make([]candidate, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(strings.ToLower(entry.Name()), ".zip") {
			continue
		}
		info, infoErr := entry.Info()
		if infoErr != nil {
			continue
		}
		full := filepath.Join(policy.Dir, entry.Name())
		ref, used := refs[localCopyKey(full)]
		candidates = append(candidates, candidate{path: full, mod: info.ModTime(), size: info.Size(), ref: ref, used: used})
	}

	remove := func(item candidate, reason string) {
		// 双保险：即使调用方判断有误，也绝不删除在跑作业的副本。
		if item.used && !item.ref.Phase.terminal() {
			return
		}
		if !policy.DryRun {
			if err := os.Remove(item.path); err != nil && !errors.Is(err, os.ErrNotExist) {
				blog("WARN", "delivery.local_clean_failed", "清理本地副本失败", "", "", "", "", map[string]any{
					"local_path": item.path, "reason": reason, "error": err.Error(),
				})
				return
			}
		}
		report.Removed++
		report.Bytes += item.size
		report.Reasons[reason]++
	}

	for _, item := range candidates {
		switch {
		case item.used && item.ref.Phase == phaseDone:
			remove(item, "delivered")
		case item.used && item.ref.Phase == phaseFailed:
			if now.Sub(item.mod) >= policy.FailureTTL {
				remove(item, "ttl")
			}
		case item.used:
			// waiting_collect / pulling / uploading / completing / retrying：正在用，绝不删。
		default:
			if now.Sub(item.mod) >= deliveryLocalCopyOrphanGrace {
				remove(item, "orphan")
			}
		}
	}

	// 总量上限：重新扫一遍真实占用（不能直接用 candidates 求和：
	// 第一轮已经删掉的文件不应再计入，dry-run 时又必须保留原值）。
	// dry-run 下跳过超量淘汰，避免基于"假设已删"的账目给出误导性的数字。
	if !policy.DryRun && policy.MaxBytes > 0 {
		report.Removed += r.evictUntilUnderCap(policy.Dir, policy.MaxBytes, refs, &report)
	}
	return report
}

// evictUntilUnderCap 按"最后修改时间最旧优先"淘汰，直到目录总量回落到上限内。
//
// 为什么最旧优先：副本的用途只有续传/重试，越旧越不可能再被用到；
// 而按大小优先会让一次大包交付顺手删掉别人刚拉好的小包。
func (r *deliveryRunner) evictUntilUnderCap(dir string, maxBytes int64, refs map[string]localCopyRef, report *localCopyCleanupReport) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	type item struct {
		path string
		mod  time.Time
		size int64
		used bool
		ref  localCopyRef
	}
	items := make([]item, 0, len(entries))
	var total int64
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(strings.ToLower(entry.Name()), ".zip") {
			continue
		}
		info, infoErr := entry.Info()
		if infoErr != nil {
			continue
		}
		full := filepath.Join(dir, entry.Name())
		ref, used := refs[localCopyKey(full)]
		items = append(items, item{path: full, mod: info.ModTime(), size: info.Size(), used: used, ref: ref})
		total += info.Size()
	}
	if total <= maxBytes {
		return 0
	}
	sort.Slice(items, func(i, j int) bool { return items[i].mod.Before(items[j].mod) })

	removed := 0
	for _, it := range items {
		if total <= maxBytes {
			break
		}
		// 已交付成功但删除失败的副本可以再试一次；在跑作业的副本不动。
		if it.used && !it.ref.Phase.terminal() {
			continue
		}
		if err := os.Remove(it.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			continue
		}
		removed++
		total -= it.size
		report.Bytes += it.size
		report.Reasons["over_cap"]++
	}
	return removed
}

// runLocalCopyCleanup 执行一轮对账并留痕（没有删除时不写事件，避免噪声）。
func (r *deliveryRunner) runLocalCopyCleanup() {
	report := r.reconcileLocalCopies(time.Now())
	if report.Removed == 0 && !report.DryRun {
		return
	}
	blog("INFO", "delivery.local_cleaned", "本地中转副本对账完成", "", "", "", "", map[string]any{
		"removed": report.Removed, "bytes": report.Bytes,
		"reasons": report.Reasons, "dry_run": report.DryRun,
	})
}

// startLocalCopyCleanup 启动对账循环。
//
// 启动后立即跑一次：老版本遗留的 GB 级副本正是最该被清掉的部分，
// 等 6 小时才动等于让用户继续踩着同一个坑。
func (r *deliveryRunner) startLocalCopyCleanup() {
	go func() {
		r.runLocalCopyCleanup()
		ticker := time.NewTicker(deliveryLocalCleanupInterval)
		defer ticker.Stop()
		for range ticker.C {
			r.runLocalCopyCleanup()
		}
	}()
}
