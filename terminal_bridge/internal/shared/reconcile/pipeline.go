package reconcile

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"terminal_bridge/internal/shared/remote"
)

// pipeline.go —— 统一编排流水线（收口设计 §三）。
//
//	Provider.Desired → Normalize → probe → state.Load → plan.Compare
//	  → guard.Acquire → fanout.WriteAll → activate.Install → activate.Smoke
//	  → state.Save → policy.OnSuccessRestore → receipt
//
// S2 先落单节点直写路径（ConfigFile 消费者）；S3 接入 guard/activate 与
// 三钩子（失败回滚 / 成功还原 / 备份保留），接入点是 Deps 上的可选字段。
// 多主机编排由消费者直接用 Fanout（spc 扇出），不在单节点 Run 里再套一层。
//
// 产品语义一律经函数注入（ProbeFunc/InstallFunc/SmokeFunc/FetchFunc）：
// descriptor 里不出现命令字符串，骨架也不认识 acli 或插件注册表。

// ProbeFunc 探测节点事实。为 nil 时用 ProbeFiles（只探文件，不探版本）。
type ProbeFunc func(ctx context.Context, r remote.Runner, d DesiredState) (NodeFact, error)

// InstallFunc 渲染并执行产品特有的安装动作（acli_install / register_plugin）。
type InstallFunc func(ctx context.Context, r remote.Runner, d DesiredState) (remote.ExecResult, error)

// SmokeFunc 渲染并执行产品特有的冒烟测试（version_match / stdout_contains）。
type SmokeFunc func(ctx context.Context, r remote.Runner, d DesiredState) (remote.ExecResult, error)

// FetchFunc 解引用 package 来源（HTTP 下载 / 平台票据端点）。
// 为 nil 时 package 来源一律 fail-closed 回 package_source_undefined，
// 不静默截断（§3.3：存储未配置时 manifest 端点拒绝服务）。
type FetchFunc func(ctx context.Context, spec FileSpec) ([]byte, error)

// Deps 是流水线的全部外部依赖。
type Deps struct {
	Runner remote.Runner
	State  *Store
	Guard  *Guard // 主机键互斥（B3）；nil 表示不互斥（单节点一次性动作）
	// Activator 是写入集与回滚/还原执行器。**必须每次 Run 新建，或留空让 Run 内部自建**。
	// 它只增不删、Entries() 返回全量，跨多次 Run 复用会把回滚/还原作用到上一次的条目
	// （其中包含删除上一轮备份，而备份是终态还原的唯一来源）。
	Activator *Activator
	// RestoreIntent 是 WaitFor=task_terminal 时要持久化的还原意图。
	// 内容由消费者给（spc 的槽位/目录/本机地址是产品语义，骨架不该认识），
	// 骨架只负责"何时落盘"——漏落盘就等于把平台清单永久留在节点上。
	RestoreIntent *RestoreIntent
	ProbeFunc     ProbeFunc
	InstallFunc   InstallFunc
	SmokeFunc     SmokeFunc
	FetchFunc     FetchFunc
	OnProgress    remote.ProgressFunc
	Compare       CompareOptions
}

// rollbackBudget 是失败回滚的独立预算。
//
// 为什么不能沿用主 ctx：主 ctx 往往正是因为断链/超时才失败的，
// 拿同一个已取消的 ctx 去回滚，一条命令都发不出去，现场永久留污染。
const rollbackBudget = 60 * time.Second

// Run 执行一次收敛，返回统一回执。
//
// 任何一步失败都带 Stage 与可判错误码；降级（探测失败、校验档位低于 sha256）
// 一律写进 Evidence 并置 Degraded，绝不把"没验过"伪装成"验过了"。
func Run(ctx context.Context, deps Deps, d DesiredState) Result {
	normalized, ev, err := Normalize(d)
	if err != nil {
		return Failed(StageDescriptor, descriptorCode(err), err.Error(), false, ev...)
	}
	d = normalized
	if deps.Runner == nil {
		return Failed(StageProbe, CodeProbeFailed, "缺少执行器（Runner），无法收敛", false, ev...)
	}

	fact, probeErr := probeNode(ctx, deps, d)
	if probeErr != nil {
		fact.Probed = false
		ev = append(ev, KV("probe_error", probeErr.Error()))
	}
	if fact.Algo != "" && fact.Algo != "sha256" {
		ev = append(ev, KV("probe_integrity", fact.Algo))
	}

	ledger, ledgerErr := loadLedger(deps, d)
	if ledgerErr != nil {
		// 账本坏了不阻断收敛：节点事实优先，只是不能凭账本 Skip。
		ev = append(ev, KV("ledger_error", ledgerErr.Error()))
	}

	plan := Compare(d, fact, ledger, deps.Compare)
	ev = append(ev, plan.Evidence...)
	switch plan.Action {
	case ActionSkip:
		return Skipped(plan.Stage, plan.Reason, plan.Degraded, ev...)
	case ActionBlockedDowngrade:
		return Blocked(StageProbe, CodeDowngradeBlocked, plan.Reason, ev...)
	}

	activator := deps.Activator
	if activator == nil {
		activator = NewActivator()
	}
	// 互斥只罩"会改节点"的段：探测是只读的，提前拿锁会让并发读白白排队。
	if deps.Guard != nil {
		release, guardErr := deps.Guard.Acquire(HostKey(d.Kind, d.NodeIP))
		if guardErr != nil {
			if errors.Is(guardErr, ErrBusy) {
				return Blocked(StageWrite, CodeBusy, "同主机同链路已有同步在进行，稍后重试", ev...)
			}
			return Failed(StageWrite, CodeBusy, guardErr.Error(), true, ev...)
		}
		defer release()
	}

	writes, writeEv, fail := writeFiles(ctx, deps, activator, d)
	ev = append(ev, writeEv...)
	if fail != nil {
		return withRollback(ctx, deps, activator, d, fail.WithEvidence(ev...))
	}

	installRes, installFail := runInstall(ctx, deps, d)
	if installFail != nil {
		return withRollback(ctx, deps, activator, d,
			installFail.WithEvidence(ev...).WithEvidence(KV("install_exit", strconv.Itoa(installRes.ExitCode))))
	}
	// 成功也要留痕：回执必须能区分"没配安装动作"与"配了且执行成功"，
	// 否则排查"到底装没装"只能再去节点上翻文件。
	installKind := d.Install.Kind
	if installKind == "" {
		installKind = InstallNone
	}
	ev = append(ev, KV("install_kind", installKind), KV("install_exit", strconv.Itoa(installRes.ExitCode)))

	smokeRes, smokeEv, fail := runSmoke(ctx, deps, d, writes)
	ev = append(ev, smokeEv...)
	if fail != nil {
		return withRollback(ctx, deps, activator, d, fail.WithEvidence(ev...))
	}
	_ = smokeRes

	if deps.State != nil {
		st := &AppliedState{
			Kind: d.Kind, NodeIP: d.NodeIP, DesiredHash: DesiredHash(d),
			AppliedAt: time.Now(), Evidence: ev,
		}
		if err := deps.State.Save(st); err != nil {
			// 账本落盘失败不推翻已成功的写入：节点事实已经是期望态，
			// 下一次凭节点事实仍能正确判定，只是少了一次快速 Skip。
			ev = append(ev, KV("ledger_save_error", err.Error()))
		}
	}

	result := OK(StageVerify, ev...)
	for _, w := range writes {
		if w.Integrity != "" && w.Integrity != "sha256" {
			result.Degraded = true
		}
	}
	if plan.Degraded {
		result.Degraded = true
	}
	restoreEv, degraded := restoreOnSuccess(ctx, deps, activator, d)
	result = result.WithEvidence(restoreEv...)
	if degraded {
		result.Degraded = true
	}
	return result
}

// withRollback 在失败回执上叠加回滚结果。
//
// 主因码不被覆盖：回滚失败是叠加风险，把它当成主因会让用户按错误的码排查
// （与 spc 扇出同一教训）。回滚只动登记过的条目，且备份缺失时拒绝删除（B1）。
func withRollback(ctx context.Context, deps Deps, activator *Activator, d DesiredState, result Result) Result {
	if d.Policy.OnFailRollback.Strategy != RollbackRestoreBackup {
		return result
	}
	entries := activator.Entries()
	if len(entries) == 0 {
		return result.WithEvidence(KV("rollback", "nothing_written"))
	}
	rollbackCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), rollbackBudget)
	defer cancel()
	if err := activator.Rollback(rollbackCtx, deps.Runner, entries); err != nil {
		return result.WithEvidence(KV("rollback", "failed"), KV("rollback_error", err.Error()))
	}
	return result.WithEvidence(KV("rollback", "restored"), KV("rollback_count", strconv.Itoa(len(entries))))
}

// restoreOnSuccess 执行成功钩子：immediate 当场还原，task_terminal 持久化意图。
//
// task_terminal 不是内存尾钩子（§3.2）：它等的是小时级、且跨越进程重启的
// 任务终态。意图落盘后，watcher 重启能从账本 + 自描述备份名恢复现场。
// 备份此时绝不能删：它就是还原的唯一来源，KeepBackup 只对 immediate 生效。
func restoreOnSuccess(ctx context.Context, deps Deps, activator *Activator, d DesiredState) ([]Evidence, bool) {
	spec := d.Policy.OnSuccessRestore
	if spec.Strategy != RestoreOriginal {
		return nil, false
	}
	entries := activator.Entries()
	if spec.WaitFor == WaitTaskTerminal {
		if deps.RestoreIntent == nil {
			// 不静默：写入已成功，但没人会在终态把原件放回去。
			// 置 Degraded + 留痕，让回执与日志都能看出"还原意图未登记"。
			return []Evidence{KV("restore_intent", "undefined")}, true
		}
		intent := *deps.RestoreIntent
		if intent.Kind == "" {
			intent.Kind = d.Kind
		}
		if intent.TaskKey == "" {
			intent.TaskKey = d.TaskKey
		}
		intent.WaitFor = WaitTaskTerminal
		if intent.CreatedAt.IsZero() {
			intent.CreatedAt = time.Now()
		}
		store := deps.State
		if store == nil {
			store = sharedStore()
		}
		if err := store.SaveIntent(intent); err != nil {
			return []Evidence{KV("restore_intent", "save_failed"), KV("restore_intent_error", err.Error())}, true
		}
		return []Evidence{KV("restore_intent", "persisted"), KV("restore_task_key", intent.TaskKey)}, false
	}

	outcomes, err := activator.Restore(ctx, deps.Runner, entries)
	restored, skipped, failed := 0, 0, 0
	for _, outcome := range outcomes {
		switch outcome.State {
		case "restored":
			restored++
		case "skipped":
			skipped++
		default:
			failed++
		}
	}
	ev := []Evidence{
		KV("restore", "immediate"),
		KV("restore_restored", strconv.Itoa(restored)),
		KV("restore_skipped", strconv.Itoa(skipped)),
		KV("restore_failed", strconv.Itoa(failed)),
	}
	degraded := err != nil || failed > 0
	if err != nil {
		ev = append(ev, KV("restore_error", err.Error()))
	}
	if !degraded && !d.Policy.KeepBackup {
		cleaned, cleanupFailed := 0, 0
		for _, entry := range entries {
			if entry.IsDirTree {
				continue // 目录树备份的清理归消费者（插件保留 .old.$SYNC_ID 供追认）
			}
			// 删除结果必须校验：没删掉却报"已清理"，与本函数头"绝不把没验过
			// 伪装成验过了"直接冲突；残留备份还会污染下一轮同名登记的判定。
			res := remote.Run(ctx, deps.Runner, "rm -f "+remote.ShellQuote(entry.BackupPath))
			if res.Err != nil || res.ExitCode != 0 {
				cleanupFailed++
				ev = append(ev, KV("backup_cleanup_failed", entry.BackupPath))
				continue
			}
			cleaned++
		}
		switch {
		case cleanupFailed > 0:
			// 部分失败不能报"已清"：残留是可见风险，必须降级并留出具体路径。
			degraded = true
			ev = append(ev, KV("backup_kept", "partial"),
				KV("backup_cleaned", strconv.Itoa(cleaned)),
				KV("backup_cleanup_failed_count", strconv.Itoa(cleanupFailed)))
		default:
			ev = append(ev, KV("backup_kept", "false"))
		}
	}
	return ev, degraded
}

func descriptorCode(err error) string {
	msg := err.Error()
	switch {
	case strings.HasPrefix(msg, CodeDescriptorTooLarge):
		return CodeDescriptorTooLarge
	case strings.Contains(msg, CodePackageSourceUndef):
		return CodePackageSourceUndef
	default:
		return CodeDescriptorInvalid
	}
}

func probeNode(ctx context.Context, deps Deps, d DesiredState) (NodeFact, error) {
	if deps.ProbeFunc != nil {
		return deps.ProbeFunc(ctx, deps.Runner, d)
	}
	return ProbeFiles(ctx, deps.Runner, d)
}

func loadLedger(deps Deps, d DesiredState) (*AppliedState, error) {
	if deps.State == nil {
		return nil, nil
	}
	return deps.State.Load(d.Kind, d.NodeIP)
}

// writeFiles 逐文件落盘：确保目录 → 解引用来源 → remote 写入（先校验再 mv）。
//
// 返回值里的 writes 供 sha256 冒烟复用：写入协议已经在 mv 后回读校验过，
// 再探一次只是多一个往返，不产生新信息。
func writeFiles(ctx context.Context, deps Deps, activator *Activator, d DesiredState) ([]remote.WriteResult, []Evidence, *Result) {
	writes := make([]remote.WriteResult, 0, len(d.Files))
	var ev []Evidence
	for _, f := range d.Files {
		if err := ctx.Err(); err != nil {
			return writes, ev, resultPtr(Failed(StageWrite, CodeWriteFailed, err.Error(), true))
		}
		if f.Dir != "" {
			if err := remote.EnsureDir(ctx, deps.Runner, f.Dir); err != nil {
				return writes, ev, resultPtr(Failed(StageWrite, CodeWriteFailed,
					fmt.Sprintf("重建目录失败 %s：%v", f.Dir, err), true))
			}
			ev = append(ev, KV("ensured_dir", f.Dir))
		}

		var wr remote.WriteResult
		var werr error
		verifySpec := remote.WriteSpec{Path: f.Path, Size: f.Size, SHA256: f.SHA256, Mode: f.Mode}
		switch f.Source.Kind {
		case SourceRemote:
			wr, werr = remote.CopyFile(ctx, deps.Runner, remote.CopySpec{
				SourcePath: f.Source.Path, Path: f.Path, Size: f.Size, SHA256: f.SHA256, Mode: f.Mode,
			})
		default:
			data, err := resolveSource(ctx, deps, f)
			if err != nil {
				return writes, ev, resultPtr(Failed(StageDownload, downloadCode(err), err.Error(), true))
			}
			// verifySpec 只喂 VerifyWrite；后者只比 size 与摘要，从不读 Data。
			// 这里不再回填原文，避免后人误以为它做了逐字节比对（那一步在 runSmoke）。
			wr, werr = remote.WriteFile(ctx, deps.Runner, remote.WriteSpec{
				Path: f.Path, Data: data, Size: f.Size, SHA256: f.SHA256, Mode: f.Mode,
				OnProgress: deps.OnProgress,
			})
		}
		// 写入原语对"脚本以约定非零码终结"返回 (result, nil)：结论在回显里，
		// 必须由调用方显式判定。漏掉这一步等于把 exit 21/22 当成写入成功。
		//
		// CopyFile 内部已自检一次 VerifyWrite，这里对 inline/remote 统一再判一次：
		// 两处都只比元数据、不产生额外往返。保留它失败归类才有唯一出口，
		// 不要当冗余删掉——删了 remote 分支就失去兜底。
		if werr == nil {
			wr.Integrity, werr = remote.VerifyWrite(verifySpec, wr)
		}
		writes = append(writes, wr)
		ev = append(ev,
			KV("write_path", f.Path),
			KV("write_size", strconv.FormatInt(wr.RemoteSize, 10)),
			KV("write_algo", wr.Algo),
			KV("write_digest", wr.Digest),
			KV("write_exit", strconv.Itoa(wr.ExitCode)),
			KV("integrity", wr.Integrity),
		)
		// 期望有摘要、节点却只给出 size_only/空结论 = 摘要工具缺失（HCI 极简宿主
		// 常见，sha256sum/md5sum 都可能没有）。单独记一条，避免只看到
		// integrity=size_only 时被误读成"校验通过"。
		if f.SHA256 != "" && wr.Integrity != "sha256" && wr.Integrity != "md5" {
			ev = append(ev, KV("digest_unavailable", f.Path))
		}
		if werr != nil {
			stage, code, retryable := StageWrite, CodeWriteFailed, true
			var we *remote.WriteError
			if errors.As(werr, &we) {
				retryable = we.Retryable
				if we.Stage == "verify" {
					stage, code = StageVerify, CodeVerifyFailed
				}
			}
			return writes, ev, resultPtr(Failed(stage, code, werr.Error(), retryable))
		}
		// 写入成功才登记：回滚只动登记过的条目，未写入的路径即使传进来也不会被碰。
		activator.Register(WriteEntry{Path: f.Path, IsDirTree: f.IsDirTree, BackupPath: f.BackupPath})
	}
	return writes, ev, nil
}

func resolveSource(ctx context.Context, deps Deps, f FileSpec) ([]byte, error) {
	switch f.Source.Kind {
	case SourceInline:
		return f.Source.Data, nil
	case SourcePackage:
		if deps.FetchFunc == nil {
			return nil, fmt.Errorf("%s：%s 的包来源未配置下载器，拒绝写入", CodePackageSourceUndef, f.Path)
		}
		return deps.FetchFunc(ctx, f)
	}
	return nil, fmt.Errorf("%s：来源 %q 不可解引用", CodeDescriptorInvalid, f.Source.Kind)
}

func downloadCode(err error) string {
	if strings.Contains(err.Error(), CodePackageSourceUndef) {
		return CodePackageSourceUndef
	}
	return CodeWriteFailed
}

// runInstall 执行安装动作。通用动作在骨架内渲染，产品动作交给注入的渲染器。
func runInstall(ctx context.Context, deps Deps, d DesiredState) (remote.ExecResult, *Result) {
	switch d.Install.Kind {
	case "", InstallNone:
		return remote.ExecResult{}, nil
	case InstallChmodExec:
		if len(d.Install.Args) < 2 {
			return remote.ExecResult{}, resultPtr(Failed(StageInstall, CodeInstallUnsupported,
				"chmod_exec 需要 mode 与至少一个路径", false))
		}
		quoted := make([]string, 0, len(d.Install.Args)-1)
		for _, p := range d.Install.Args[1:] {
			quoted = append(quoted, remote.ShellQuote(p))
		}
		command := "chmod " + remote.ShellQuote(d.Install.Args[0]) + " " + strings.Join(quoted, " ")
		res := remote.Run(ctx, deps.Runner, command)
		if res.Err != nil || res.ExitCode != 0 {
			return res, resultPtr(Failed(StageInstall, CodeInstallFailed,
				fmt.Sprintf("chmod 失败（exit=%d）：%v", res.ExitCode, res.Err), true))
		}
		return res, nil
	default:
		if deps.InstallFunc == nil {
			return remote.ExecResult{}, resultPtr(Failed(StageInstall, CodeInstallUnsupported,
				fmt.Sprintf("安装动作 %q 未登记渲染器", d.Install.Kind), false))
		}
		res, err := deps.InstallFunc(ctx, deps.Runner, d)
		if err != nil || res.ExitCode != 0 {
			detail := fmt.Sprintf("安装命令失败（exit=%d）", res.ExitCode)
			if err != nil {
				detail += "：" + err.Error()
			}
			return res, resultPtr(Failed(StageInstall, CodeInstallFailed, detail, true))
		}
		return res, nil
	}
}

// runSmoke 证明"写下去的就是期望态"。
//
// sha256 档由骨架内建：写入协议已在 mv 后回读校验，直接复用其结论；
// 档位低于 sha256（宿主机极简，sha256sum/md5sum 都可能缺失）时降级为
// printf 重传原文 + cmp 逐字节比对——**不做"探测不了就跳过"**，
// 否则一次写坏的配置会以"任务成功但少收"收场。
func runSmoke(ctx context.Context, deps Deps, d DesiredState, writes []remote.WriteResult) (remote.ExecResult, []Evidence, *Result) {
	var ev []Evidence
	switch d.Smoke.Kind {
	case "", SmokeNone:
		return remote.ExecResult{}, ev, nil
	case SmokeSHA256:
		for i, f := range d.Files {
			if f.SHA256 == "" {
				continue
			}
			if i < len(writes) && writes[i].Integrity == "sha256" && strings.EqualFold(writes[i].Digest, f.SHA256) {
				ev = append(ev, KV("smoke", "sha256"), KV("smoke_path", f.Path))
				continue
			}
			if f.Source.Kind != SourceInline || len(f.Source.Data) == 0 {
				// 把写入阶段的完整性档位带出来：真因往往是节点缺 sha256sum/md5sum
				// （integrity=size_only），而不是"回读内容不一致"。不带上档位，
				// 定责会一律偏向校验层，掩盖节点工具缺失。
				integrity := "unknown"
				if i < len(writes) && writes[i].Integrity != "" {
					integrity = writes[i].Integrity
				}
				return remote.ExecResult{}, ev, resultPtr(Failed(StageVerify, CodeVerifyFailed,
					fmt.Sprintf("回读校验失败：%s 的完整性档位为 %s（期望 sha256），且来源非内联无法逐字节比对", f.Path, integrity), true))
			}
			command := "printf '%s' " + remote.ShellQuote(string(f.Source.Data)) + " | cmp -s - " + remote.ShellQuote(f.Path)
			res := remote.Run(ctx, deps.Runner, command)
			ev = append(ev, KV("smoke", "cmp"), KV("smoke_path", f.Path), KV("smoke_exit", strconv.Itoa(res.ExitCode)))
			if res.Err != nil || res.ExitCode != 0 {
				return res, ev, resultPtr(Failed(StageVerify, CodeVerifyFailed,
					fmt.Sprintf("exit=%d：%v", res.ExitCode, res.Err), true))
			}
		}
		return remote.ExecResult{}, ev, nil
	default:
		if deps.SmokeFunc == nil {
			// 用 verify 域自己的码：install_unsupported 会让排查误入安装段
			// （同一个码在 runInstall 里另指"安装动作无渲染器"）。
			return remote.ExecResult{}, ev, resultPtr(Failed(StageVerify, CodeVerifyFailed,
				fmt.Sprintf("冒烟测试 %q 未登记渲染器", d.Smoke.Kind), false))
		}
		res, err := deps.SmokeFunc(ctx, deps.Runner, d)
		ev = append(ev, KV("smoke", d.Smoke.Kind), KV("smoke_exit", strconv.Itoa(res.ExitCode)))
		if err != nil || res.ExitCode != 0 {
			detail := fmt.Sprintf("冒烟测试失败（exit=%d）", res.ExitCode)
			if err != nil {
				detail += "：" + err.Error()
			}
			return res, ev, resultPtr(Failed(StageVerify, CodeVerifyFailed, detail, true))
		}
		return res, ev, nil
	}
}

func resultPtr(r Result) *Result { return &r }
