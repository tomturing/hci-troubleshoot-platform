package reconcile

// pipeline_policy_test.go —— S3 验收：三钩子在流水线上的真实行为。
//
// 用真 shell 跑完整 Run：失败回滚要把原件放回去、成功还原要按 WaitFor 分流
// （immediate 当场还原 / task_terminal 持久化意图）。这些分支只有跑过才知道
// "回滚用的 ctx 是不是已经被取消了"、"备份是不是被提前删了"。

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"terminal_bridge/internal/shared/remote"
)

const (
	policyOriginal = "[{\"action\":\"copy\",\"path\":\"/sf/log/orig\"}]\n"
	policyPlatform = "[{\"action\":\"copy\",\"path\":\"/sf/log/platform\"}]\n"
)

// policyFixture 造一份"节点上已有原件 + 备份已按任务隔离建好"的现场。
// 备份先建是 spc prepare 阶段的语义：写前自愈 → 备份 → 校验，之后才轮到写入。
func policyFixture(t *testing.T) (dir, target, backup string, runner *shellRunner) {
	t.Helper()
	requireShell(t, "cp", "cmp", "rm", "mv", "sha256sum")
	dir = t.TempDir()
	target = filepath.Join(dir, "core.json")
	backup = target + ".platform.task-1.bak"
	writeFile(t, target, policyOriginal, 0o644)
	writeFile(t, backup, policyOriginal, 0o644)
	return dir, target, backup, &shellRunner{}
}

// policyDesired 默认 Install=none（成功路径）；失败路径用 withInstall 换成
// 产品动作，否则 runInstall 根本不会调 InstallFunc，测不到安装失败分支。
func policyDesired(target, backup string, policy ReconcilePolicy) DesiredState {
	return DesiredState{
		Kind: KindSpcSlot, NodeIP: "172.28.24.1", TaskKey: "task-1",
		Files: []FileSpec{{
			Path: target, Mode: "0644", BackupPath: backup,
			Source: Source{Kind: SourceInline, Data: []byte(policyPlatform)},
		}},
		Install: InstallSpec{Kind: InstallNone},
		Smoke:   SmokeSpec{Kind: SmokeSHA256},
		Policy:  policy,
		Version: VersionSpec{Mode: VersionNone},
	}
}

// withInstall 把期望态换成"需要产品渲染器的安装动作"，用于构造安装失败。
func withInstall(d DesiredState) DesiredState {
	d.Install = InstallSpec{Kind: InstallAcli}
	return d
}

func failingInstall(ctx context.Context, r remote.Runner, d DesiredState) (remote.ExecResult, error) {
	return remote.ExecResult{ExitCode: 1}, nil
}

// TestPipelineRollsBackOnInstallFailure 安装失败必须把原件放回去，
// 且主因码不被回滚结果覆盖（回滚失败是叠加风险，不是主因）。
func TestPipelineRollsBackOnInstallFailure(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := withInstall(policyDesired(target, backup, ReconcilePolicy{
		OnFailRollback: RollbackSpec{Strategy: RollbackRestoreBackup},
	}))
	result := Run(context.Background(), Deps{
		Runner:      runner,
		InstallFunc: failingInstall,
	}, desired)
	if result.Status != StatusError || result.Stage != StageInstall {
		t.Fatalf("安装失败应如实回执：%+v", result)
	}
	if result.Field("rollback") != "restored" {
		t.Fatalf("失败后必须回滚已写入条目：%+v", result.Evidence)
	}
	if got := readLocalFile(t, target); got != policyOriginal {
		t.Fatalf("回滚后盘上应是原件，实际 %q", got)
	}
	if _, err := os.Stat(backup); err != nil {
		t.Fatalf("回滚不该消费掉备份（终态还原还要用）：%v", err)
	}
}

// TestPipelineRollbackUsesFreshContext 主 ctx 已取消时回滚仍要能发命令：
// 沿用已取消的 ctx 会让回滚一条都发不出去，现场永久留平台清单。
func TestPipelineRollbackUsesFreshContext(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := withInstall(policyDesired(target, backup, ReconcilePolicy{
		OnFailRollback: RollbackSpec{Strategy: RollbackRestoreBackup},
	}))
	ctx, cancel := context.WithCancel(context.Background())
	result := Run(ctx, Deps{
		Runner: runner,
		InstallFunc: func(ctx context.Context, r remote.Runner, d DesiredState) (remote.ExecResult, error) {
			cancel() // 安装动作把主 ctx 弄死（断链/超时的真实形态）
			return remote.ExecResult{ExitCode: 1}, nil
		},
	}, desired)
	if result.Field("rollback") != "restored" {
		t.Fatalf("主 ctx 取消后回滚仍必须执行：%+v", result.Evidence)
	}
	if got := readLocalFile(t, target); got != policyOriginal {
		t.Fatalf("原件未被放回：%q", got)
	}
}

func TestPipelineNoRollbackWhenPolicyNone(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := withInstall(policyDesired(target, backup, ReconcilePolicy{}))
	result := Run(context.Background(), Deps{
		Runner:      runner,
		InstallFunc: failingInstall,
	}, desired)
	if result.Status != StatusError {
		t.Fatalf("应失败：%+v", result)
	}
	if result.Field("rollback") != "" {
		t.Fatalf("策略为 none 时不该回滚：%+v", result.Evidence)
	}
	if got := readLocalFile(t, target); got != policyPlatform {
		t.Fatalf("none 策略下盘上应保持已写入内容：%q", got)
	}
}

// TestPipelineImmediateRestoreOnSuccess immediate 还原当场把原件放回去，
// KeepBackup=false 时备份随之清理（插件保留 .old 是另一条策略）。
func TestPipelineImmediateRestoreOnSuccess(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := policyDesired(target, backup, ReconcilePolicy{
		OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitImmediate},
	})
	result := Run(context.Background(), Deps{Runner: runner}, desired)
	if result.Status != StatusOK {
		t.Fatalf("写入+校验应成功：%+v", result)
	}
	if result.Field("restore") != "immediate" || result.Field("restore_restored") != "1" {
		t.Fatalf("immediate 还原证据缺失：%+v", result.Evidence)
	}
	if got := readLocalFile(t, target); got != policyOriginal {
		t.Fatalf("成功后应已还原原件：%q", got)
	}
	if _, err := os.Stat(backup); !os.IsNotExist(err) {
		t.Fatalf("KeepBackup=false 时备份应被清理：%v", err)
	}
}

func TestPipelineKeepBackupRetainsFile(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := policyDesired(target, backup, ReconcilePolicy{
		OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitImmediate},
		KeepBackup:       true,
	})
	result := Run(context.Background(), Deps{Runner: runner}, desired)
	if result.Status != StatusOK {
		t.Fatalf("应成功：%+v", result)
	}
	if _, err := os.Stat(backup); err != nil {
		t.Fatalf("KeepBackup=true 时备份必须保留：%v", err)
	}
	if result.Field("backup_kept") != "" {
		t.Fatalf("保留备份时不该记 backup_kept=false：%+v", result.Evidence)
	}
}

// TestPipelineTaskTerminalPersistsIntent 是 §3.2 的核心：task_terminal 不是
// 内存尾钩子。流水线返回前必须把还原意图落盘，且**当场不还原、不删备份**——
// 采集还没跑，原件现在放回去等于用原清单采集。
func TestPipelineTaskTerminalPersistsIntent(t *testing.T) {
	stateDir, target, backup, runner := policyFixture(t)
	store := NewStore(filepath.Join(stateDir, "state"))
	desired := policyDesired(target, backup, ReconcilePolicy{
		OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitTaskTerminal},
	})
	result := Run(context.Background(), Deps{
		Runner: runner, State: store,
		RestoreIntent: &RestoreIntent{
			NodeIPs: []string{"172.28.24.1", "172.28.25.1"},
			Slots:   []string{"core"},
			Dirs:    map[string]string{"172.28.24.1": filepath.Dir(target)},
		},
	}, desired)
	if result.Status != StatusOK || result.Degraded {
		t.Fatalf("登记意图后应是干净的成功回执：%+v", result)
	}
	if result.Field("restore_intent") != "persisted" || result.Field("restore_task_key") != "task-1" {
		t.Fatalf("意图落盘证据缺失：%+v", result.Evidence)
	}
	if got := readLocalFile(t, target); got != policyPlatform {
		t.Fatalf("task_terminal 下当场不得还原：%q", got)
	}
	if _, err := os.Stat(backup); err != nil {
		t.Fatalf("备份是终态还原的唯一来源，绝不能提前删：%v", err)
	}
	// 重启后仍能凭意图 + 自描述备份名收尾。
	intents, err := NewStore(filepath.Join(stateDir, "state")).LoadIntents()
	if err != nil || len(intents) != 1 {
		t.Fatalf("重启后应读回意图：%+v err=%v", intents, err)
	}
	if intents[0].Kind != KindSpcSlot || intents[0].TaskKey != "task-1" || intents[0].WaitFor != WaitTaskTerminal {
		t.Fatalf("意图主键/时机丢失：%+v", intents[0])
	}
	if len(intents[0].NodeIPs) != 2 || len(intents[0].Slots) != 1 {
		t.Fatalf("消费者给的还原范围丢失：%+v", intents[0])
	}
}

func TestPipelineTaskTerminalWithoutIntentIsDegraded(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := policyDesired(target, backup, ReconcilePolicy{
		OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitTaskTerminal},
	})
	result := Run(context.Background(), Deps{Runner: runner}, desired)
	if result.Status != StatusOK || !result.Degraded {
		t.Fatalf("没人登记还原意图必须显式降级，不能静默：%+v", result)
	}
	if result.Field("restore_intent") != "undefined" {
		t.Fatalf("降级原因必须留痕：%+v", result.Evidence)
	}
}

// TestPipelineGuardBusyIsBlockedNotFailed B3：同主机同链路已有同步在进行时，
// 回执必须是 blocked + retryable（前端重新入队），不是失败终态。
func TestPipelineGuardBusyIsBlockedNotFailed(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	guard := NewGuard("")
	release, err := guard.Acquire(HostKey(KindSpcSlot, "172.28.24.1"))
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	desired := policyDesired(target, backup, ReconcilePolicy{})
	result := Run(context.Background(), Deps{Runner: runner, Guard: guard}, desired)
	if result.Status != StatusBlocked || result.ErrorCode != CodeBusy || !result.Retryable {
		t.Fatalf("busy 必须是可重试的 blocked：%+v", result)
	}
	// 互斥只罩"会改节点"的段（设计 §三：probe → compare → guard.Acquire → write），
	// 只读探测允许已经发生；但一条写入命令都不得下发。
	if hasCommand(runner.commands(), "cat > ") || hasCommand(runner.commands(), "mv -f") {
		t.Fatalf("被互斥挡住时不得下发写入命令：%v", runner.commands())
	}
	if got := readLocalFile(t, target); got != policyOriginal {
		t.Fatalf("现场被改动了：%q", got)
	}
}

func TestPipelineGuardReleasedAfterRun(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	guard := NewGuard("")
	desired := policyDesired(target, backup, ReconcilePolicy{})
	if result := Run(context.Background(), Deps{Runner: runner, Guard: guard}, desired); result.Status != StatusOK {
		t.Fatalf("首次应成功：%+v", result)
	}
	if guard.Held(HostKey(KindSpcSlot, "172.28.24.1")) {
		t.Fatal("Run 返回后必须释放主机锁，否则后续同步永远 busy")
	}
	// 失败路径同样要释放（defer 不能只挂在成功分支上）。
	writeFile(t, target, policyOriginal, 0o644)
	failResult := Run(context.Background(), Deps{
		Runner:      runner,
		Guard:       guard,
		InstallFunc: failingInstall,
	}, withInstall(desired))
	if failResult.Status != StatusError {
		t.Fatalf("应失败：%+v", failResult)
	}
	if guard.Held(HostKey(KindSpcSlot, "172.28.24.1")) {
		t.Fatal("失败路径也必须释放主机锁")
	}
}

// TestPipelineRollbackRefusalIsReported 备份缺失时回滚被拒：主因码保持安装失败，
// 叠加 rollback=failed 证据，绝不能报成"已回滚"。
func TestPipelineRollbackRefusalIsReported(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	if err := os.Remove(backup); err != nil {
		t.Fatal(err)
	}
	desired := withInstall(policyDesired(target, backup, ReconcilePolicy{
		OnFailRollback: RollbackSpec{Strategy: RollbackRestoreBackup},
	}))
	result := Run(context.Background(), Deps{
		Runner:      runner,
		InstallFunc: failingInstall,
	}, desired)
	if result.Stage != StageInstall || result.ErrorCode != CodeInstallFailed {
		t.Fatalf("主因码不得被回滚结果覆盖：%+v", result)
	}
	if result.Field("rollback") != "failed" {
		t.Fatalf("无备份时回滚必须如实报失败：%+v", result.Evidence)
	}
	if got := readLocalFile(t, target); got != policyPlatform {
		t.Fatalf("无备份不得删除/改写现场：%q", got)
	}
}

func TestPipelineIntentSaveFailureIsDegraded(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	// 账本目录指向一个普通文件：MkdirAll 必然失败，模拟 PVC 不可写。
	blocker := filepath.Join(t.TempDir(), "not-a-dir")
	writeFile(t, blocker, "x", 0o600)
	desired := policyDesired(target, backup, ReconcilePolicy{
		OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitTaskTerminal},
	})
	result := Run(context.Background(), Deps{
		Runner: runner, State: NewStore(blocker),
		RestoreIntent: &RestoreIntent{NodeIPs: []string{"172.28.24.1"}},
	}, desired)
	if !result.Degraded || result.Field("restore_intent") != "save_failed" {
		t.Fatalf("意图落盘失败必须降级留痕（否则没人收尾）：%+v", result)
	}
	if _, err := os.Stat(backup); err != nil {
		t.Fatalf("备份仍须保留供人工兜底：%v", err)
	}
}

// --- 回执证据与失败归因回归 ---

// cleanupFailRunner 只拦"删备份"这一条 rm：写入/还原照常走真 shell。
// 目的是验证"没删掉就不能报已清理"，而不是模拟整条链路失败。
type cleanupFailRunner struct{ shellRunner }

func isBackupCleanup(command string) bool {
	trimmed := strings.TrimSpace(command)
	return strings.HasPrefix(trimmed, "rm -f ") && strings.Contains(trimmed, ".bak")
}

func (r *cleanupFailRunner) RunQuickCommandStreams(ctx context.Context, command string) (string, string, int, error) {
	if isBackupCleanup(command) {
		r.record(command)
		return "", "cleanup blocked", 1, nil
	}
	return r.shellRunner.RunQuickCommandStreams(ctx, command)
}

func (r *cleanupFailRunner) RunQuickCommand(ctx context.Context, command string) (string, int, error) {
	if isBackupCleanup(command) {
		r.record(command)
		return "cleanup blocked", 1, nil
	}
	return r.shellRunner.RunQuickCommand(ctx, command)
}

// TestPipelineInstallEvidenceOnSuccess 成功回执必须能区分"没配安装动作"与"装过"：
// 只有失败路径才记 install_exit 的话，排查"到底装没装"只能再去节点翻文件。
func TestPipelineInstallEvidenceOnSuccess(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := policyDesired(target, backup, ReconcilePolicy{})
	result := Run(context.Background(), Deps{Runner: runner}, desired)
	if result.Status != StatusOK {
		t.Fatalf("应成功：%+v", result)
	}
	if result.Field("install_kind") != InstallNone || result.Field("install_exit") != "0" {
		t.Fatalf("成功路径缺安装证据：%+v", result.Evidence)
	}
}

// TestPipelineBackupCleanupFailureIsDegraded 备份没删掉却报"已清理"等于自欺；
// 必须降级并留出具体路径，供现场人工收尾。
func TestPipelineBackupCleanupFailureIsDegraded(t *testing.T) {
	_, target, backup, _ := policyFixture(t)
	runner := &cleanupFailRunner{}
	desired := policyDesired(target, backup, ReconcilePolicy{
		OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitImmediate},
	})
	result := Run(context.Background(), Deps{Runner: runner}, desired)
	if result.Status != StatusOK {
		t.Fatalf("写入与还原应成功，仅清理降级：%+v", result)
	}
	if !result.Degraded {
		t.Fatalf("备份清理失败必须置 Degraded：%+v", result)
	}
	if result.Field("backup_kept") != "partial" {
		t.Fatalf("不得把清理失败报成 backup_kept=false：%+v", result.Evidence)
	}
	if result.Field("backup_cleanup_failed") != backup {
		t.Fatalf("必须留下清理失败的具体路径：%+v", result.Evidence)
	}
	if _, err := os.Stat(backup); err != nil {
		t.Fatalf("替身只是拒绝删除，备份应仍在：%v", err)
	}
}

// TestPipelineSmokeUnregisteredRendererUsesVerifyCode 冒烟渲染器缺失必须归
// verify 域，不能再借 install_unsupported（否则排查会误入安装段）。
func TestPipelineSmokeUnregisteredRendererUsesVerifyCode(t *testing.T) {
	_, target, backup, runner := policyFixture(t)
	desired := policyDesired(target, backup, ReconcilePolicy{})
	desired.Smoke = SmokeSpec{Kind: "unregistered_kind"}
	result := Run(context.Background(), Deps{Runner: runner}, desired)
	if result.Status != StatusError || result.Stage != StageVerify {
		t.Fatalf("应回执 verify 段失败：%+v", result)
	}
	if result.ErrorCode != CodeVerifyFailed {
		t.Fatalf("应归 verify_failed，实际 %q", result.ErrorCode)
	}
	if result.Retryable {
		t.Fatalf("缺渲染器是能力问题，不该标可重试：%+v", result)
	}
}
