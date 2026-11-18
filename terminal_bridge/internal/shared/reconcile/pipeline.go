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
// S2 先落单节点直写路径（ConfigFile 消费者）；guard/fanout/activate 在 S3
// 接入，接入点是 Deps 上的可选字段，不需要改流水线本身。
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
	Runner      remote.Runner
	State       *Store
	ProbeFunc   ProbeFunc
	InstallFunc InstallFunc
	SmokeFunc   SmokeFunc
	FetchFunc   FetchFunc
	OnProgress  remote.ProgressFunc
	Compare     CompareOptions
}

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

	writes, writeEv, fail := writeFiles(ctx, deps, d)
	ev = append(ev, writeEv...)
	if fail != nil {
		return fail.WithEvidence(ev...)
	}

	if res, fail := runInstall(ctx, deps, d); fail != nil {
		return fail.WithEvidence(ev...).WithEvidence(KV("install_exit", strconv.Itoa(res.ExitCode)))
	}

	smokeRes, smokeEv, fail := runSmoke(ctx, deps, d, writes)
	ev = append(ev, smokeEv...)
	if fail != nil {
		return fail.WithEvidence(ev...)
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
	return result
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
func writeFiles(ctx context.Context, deps Deps, d DesiredState) ([]remote.WriteResult, []Evidence, *Result) {
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
			verifySpec.Data = data
			wr, werr = remote.WriteFile(ctx, deps.Runner, remote.WriteSpec{
				Path: f.Path, Data: data, Size: f.Size, SHA256: f.SHA256, Mode: f.Mode,
				OnProgress: deps.OnProgress,
			})
		}
		// 写入原语对"脚本以约定非零码终结"返回 (result, nil)：结论在回显里，
		// 必须由调用方显式判定。漏掉这一步等于把 exit 21/22 当成写入成功。
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
				return remote.ExecResult{}, ev, resultPtr(Failed(StageVerify, CodeVerifyFailed,
					fmt.Sprintf("回读校验失败：%s 无 sha256 结论且原文不可用于逐字节比对", f.Path), true))
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
			return remote.ExecResult{}, ev, resultPtr(Failed(StageVerify, CodeInstallUnsupported,
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
