package reconcile

import (
	"context"
	"fmt"
	"strings"

	"terminal_bridge/internal/shared/remote"
)

// plan.go —— 期望 vs 实际的三态判定（收口设计 §三）。
//
//	Skip              已是最新 / 无修改
//	BlockedDowngrade  版本回退，拒绝
//	Install           新增 / 有修改
//
// 两条硬规则：
//  1. Skip 必须"账本对齐 且 节点事实对齐"（B4：只信账本会在节点被第三方
//     清空后永久 Skip）；
//  2. 探测失败才允许凭账本降级 Skip，且回执必须带 degraded=true 与
//     stage=probe，前端不得显示"已是最新"（B6）。

// Action 是判定结论。
type Action string

const (
	ActionSkip             Action = "skip"
	ActionInstall          Action = "install"
	ActionBlockedDowngrade Action = "blocked_downgrade"
)

// Plan 是一次判定的完整结论（含可解释证据）。
type Plan struct {
	Action   Action
	Reason   string
	Degraded bool
	Stage    string
	Evidence []Evidence
}

// CompareOptions 注入产品语义。
type CompareOptions struct {
	// Force 为真时无条件 Install（acli 的 force / 用户显式重试）。
	Force bool
	// CompareVersion 是 ordered 模式的判序函数：
	// 返回 -1 表示节点更旧（需升级）、0 表示相等、+1 表示节点更新（禁降级）。
	// acli 的"版本优先 + 日期兜底 + 禁降级"留在调用方，骨架不认识具体产品。
	// 为 nil 时 ordered 模式退化为"字符串相等即对齐"，并记 degraded 证据。
	CompareVersion func(fact NodeFact, want VersionSpec) int
	// NodeOnly 标识无账本链路（acli_sync）：节点事实是唯一真相。
	// 节点对齐即 Skip，不为建账本补一次 Apply；期望证据缺失（Probed=false，
	// 如官方源探测失败/包名解析落空）时降级 Skip 并标 Degraded——不得把
	// "没判定"伪装成"已是最新"（B6）。唯一例外：ordered 模式下节点版本为空
	// 是"明确未安装"，无论期望证据是否可得都必须 Install。
	NodeOnly bool
}

// Compare 判定期望态与现场事实/账本的差异。
func Compare(d DesiredState, fact NodeFact, ledger *AppliedState, opts CompareOptions) Plan {
	hash := DesiredHash(d)
	ledgerAligned := ledger != nil && ledger.DesiredHash != "" && ledger.DesiredHash == hash
	ev := []Evidence{
		KV("desired_hash", hash),
		KV("ledger_aligned", fmt.Sprintf("%t", ledgerAligned)),
		KV("node_probed", fmt.Sprintf("%t", fact.Probed)),
	}

	if opts.Force {
		return Plan{Action: ActionInstall, Reason: "force", Stage: StageProbe,
			Evidence: append(ev, KV("reason", "force"))}
	}

	// 总是覆写的链路（config/spc 按 task 隔离）不做差量判定：
	// 它们的"幂等"由任务隔离保证，而不是由"上次写过"保证。
	if d.Version.Mode == "" || d.Version.Mode == VersionNone {
		return Plan{Action: ActionInstall, Reason: "always_overwrite", Stage: StageProbe,
			Evidence: append(ev, KV("reason", "always_overwrite"))}
	}

	// 探测失败：字段一律不可信。此时才允许凭账本快速 Skip，但必须标降级。
	if !fact.Probed {
		if opts.NodeOnly {
			if d.Version.Mode == VersionOrdered && strings.TrimSpace(fact.Version) == "" {
				return Plan{Action: ActionInstall, Reason: "not_installed", Degraded: true, Stage: StageProbe,
					Evidence: append(ev, KV("degraded", "true"), KV("reason", "not_installed"))}
			}
			// 无账本可凭、期望又不可知：降级 Skip，调用方必须如实标注"未做升级判定"。
			return Plan{Action: ActionSkip, Reason: "probe_failed_no_desired", Degraded: true, Stage: StageProbe,
				Evidence: append(ev, KV("degraded", "true"), KV("reason", "probe_failed_no_desired"))}
		}
		if ledgerAligned {
			return Plan{Action: ActionSkip, Reason: "probe_failed_ledger_aligned", Degraded: true, Stage: StageProbe,
				Evidence: append(ev, KV("degraded", "true"), KV("reason", "probe_failed_ledger_aligned"))}
		}
		return Plan{Action: ActionInstall, Reason: "probe_failed_no_ledger", Degraded: true, Stage: StageProbe,
			Evidence: append(ev, KV("degraded", "true"), KV("reason", "probe_failed_no_ledger"))}
	}

	factAligned, reason, blocked := alignFact(d, fact, opts)
	ev = append(ev, KV("node_aligned", fmt.Sprintf("%t", factAligned)), KV("node_reason", reason))
	if blocked {
		return Plan{Action: ActionBlockedDowngrade, Reason: reason, Stage: StageProbe,
			Evidence: append(ev, KV("reason", reason))}
	}
	if !factAligned {
		// 节点事实优先于账本与云端 spec：探测成功且与期望不一致 → 一律 Apply，
		// 忽略账本"已同步"（acli 重装清空插件目录后必须重新收敛）。
		return Plan{Action: ActionInstall, Reason: reason, Stage: StageProbe,
			Evidence: append(ev, KV("reason", reason))}
	}
	if !ledgerAligned {
		if opts.NodeOnly {
			// 无账本链路：节点对齐即对齐，不存在"补建账本"的 Apply。
			return Plan{Action: ActionSkip, Reason: "up_to_date", Stage: StageProbe,
				Evidence: append(ev, KV("reason", "up_to_date"))}
		}
		// 节点已对齐但账本缺失/不符：补一次 Apply 把账本建起来。
		// 宁可多做一次幂等写入，也不要让"账本丢了"变成永久不可判定的状态。
		return Plan{Action: ActionInstall, Reason: "ledger_missing_node_aligned", Stage: StageProbe,
			Evidence: append(ev, KV("reason", "ledger_missing_node_aligned"))}
	}
	return Plan{Action: ActionSkip, Reason: "up_to_date", Stage: StageProbe,
		Evidence: append(ev, KV("reason", "up_to_date"))}
}

// alignFact 判定节点事实是否与期望对齐，返回 (对齐, 原因, 是否禁降级)。
func alignFact(d DesiredState, fact NodeFact, opts CompareOptions) (bool, string, bool) {
	switch d.Version.Mode {
	case VersionOrdered:
		if strings.TrimSpace(fact.Version) == "" {
			return false, "not_installed", false
		}
		if opts.CompareVersion == nil {
			if fact.Version == d.Version.Expected {
				return true, "version_equal_no_comparator", false
			}
			return false, "version_differs_no_comparator", false
		}
		switch opts.CompareVersion(fact, d.Version) {
		case 1:
			return false, "downgrade_blocked", true
		case 0:
			return true, "version_up_to_date", false
		default:
			return false, "version_outdated", false
		}
	case VersionHash:
		if strings.TrimSpace(d.Version.Expected) == "" {
			return false, "spec_hash_missing", false
		}
		if strings.EqualFold(strings.TrimSpace(fact.FileDigest), strings.TrimSpace(d.Version.Expected)) {
			return true, "spec_hash_equal", false
		}
		if !fact.FileExists {
			return false, "plugin_absent", false
		}
		return false, "spec_hash_differs", false
	case VersionDigest:
		for _, f := range d.Files {
			ff, ok := fileFactFor(fact, f.Path, len(d.Files))
			if !ok || !ff.Exists {
				return false, "file_absent:" + f.Path, false
			}
			if f.Size > 0 && ff.Size != f.Size {
				return false, "size_differs:" + f.Path, false
			}
			if f.SHA256 != "" && !strings.EqualFold(ff.Digest, f.SHA256) {
				return false, "digest_differs:" + f.Path, false
			}
		}
		return true, "files_aligned", false
	}
	return false, "unknown_version_mode", false
}

// fileFactFor 取某路径的事实：多文件用 Files 表，单文件兼容顶层字段。
func fileFactFor(fact NodeFact, path string, fileCount int) (FileFact, bool) {
	if fact.Files != nil {
		ff, ok := fact.Files[path]
		return ff, ok
	}
	if fileCount == 1 {
		return FileFact{Exists: fact.FileExists, Size: fact.FileSize, Digest: fact.FileDigest, Algo: fact.Algo}, true
	}
	return FileFact{}, false
}

// ProbeFiles 是默认的节点事实探测：逐文件 remote.ProbeFile。
//
// 任一文件探测失败即整体标 Probed=false——"部分探测成功"不足以支撑 Skip，
// 拿半套事实做判定等于把没验过的路径当成验过了。
// ordered 模式还需要版本事实，由调用方经 Deps.ProbeFunc 补齐（产品命令不进骨架）。
func ProbeFiles(ctx context.Context, r remote.Runner, d DesiredState) (NodeFact, error) {
	fact := NodeFact{Files: make(map[string]FileFact, len(d.Files))}
	if r == nil {
		return fact, fmt.Errorf("缺少执行器，无法探测节点事实")
	}
	for _, f := range d.Files {
		res, err := remote.ProbeFile(ctx, r, f.Path)
		if err != nil {
			fact.Probed = false
			return fact, err
		}
		ff := FileFact{Exists: !res.Missing, Size: res.Size, Digest: res.Digest, Algo: res.Algo}
		fact.Files[f.Path] = ff
		if len(d.Files) == 1 {
			fact.FileExists, fact.FileSize, fact.FileDigest, fact.Algo = ff.Exists, ff.Size, ff.Digest, ff.Algo
		}
	}
	fact.Probed = true
	return fact, nil
}
