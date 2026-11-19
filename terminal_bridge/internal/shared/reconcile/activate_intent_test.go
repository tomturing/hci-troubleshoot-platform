package reconcile

// activate_intent_test.go —— S3 验收：构造性回滚不变量（B1）与持久还原意图（§3.2）。
//
// B1 的验收必须是"真删除跑一遍"：备份不存在时现场目录到底还在不在，
// 只有真 shell 能回答。因此这里用 shellRunner + t.TempDir()，
// 而不是断言命令字符串里有没有 if。

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestActivatorRollbackSkipsUnregisteredEntries(t *testing.T) {
	requireShell(t, "cp", "cmp", "rm")
	dir := t.TempDir()
	target := filepath.Join(dir, "core.json")
	writeFile(t, target, "平台清单", 0o644)
	runner := &shellRunner{}
	activator := NewActivator()
	// 传入一个从未登记的路径：即使它真实存在，也绝不能被碰。
	err := activator.Rollback(context.Background(), runner, []WriteEntry{{Path: target, BackupPath: target + ".bak"}})
	if err != nil {
		t.Fatalf("未登记条目应被跳过而不是报错：%v", err)
	}
	if len(runner.commands()) != 0 {
		t.Fatalf("未登记条目不该下发任何命令：%v", runner.commands())
	}
	if readLocalFile(t, target) != "平台清单" {
		t.Fatal("未登记文件被改动了")
	}
}

func TestActivatorRefusesEntryWithoutBackupPath(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "core.json")
	activator := NewActivator()
	activator.Register(WriteEntry{Path: target}) // 登记了但没备份名
	_, err := activator.AssertRestorable([]WriteEntry{{Path: target, BackupPath: ""}})
	if err == nil || !strings.Contains(err.Error(), "回滚前提不满足") {
		t.Fatalf("备份名为空必须拒绝删除，实际 %v", err)
	}
	// 调用方传一个假备份名也不能绕过：以登记值为准。
	_, err = activator.AssertRestorable([]WriteEntry{{Path: target, BackupPath: "/tmp/fake.bak"}})
	if err == nil {
		t.Fatal("调用方补一个备份名就绕过了不变量")
	}
}

// TestActivatorRollbackRefusesMissingBackup 是 B1 的核心用例：
// 锁超时/备份失败都发生在目录备份之前，此时现场是唯一副本，删了就再也回不来。
func TestActivatorRollbackRefusesMissingBackup(t *testing.T) {
	requireShell(t, "cp", "cmp", "rm")
	dir := t.TempDir()
	target := filepath.Join(dir, "core.json")
	backup := target + ".platform.task1.bak"
	writeFile(t, target, "现场唯一副本", 0o644)
	runner := &shellRunner{}
	activator := NewActivator()
	activator.Register(WriteEntry{Path: target, BackupPath: backup})

	err := activator.Rollback(context.Background(), runner, []WriteEntry{{Path: target, BackupPath: backup}})
	if !errors.Is(err, ErrNoBackup) {
		t.Fatalf("备份缺失必须报错（而不是假装撤销成功），实际 %v", err)
	}
	if _, statErr := os.Stat(target); statErr != nil {
		t.Fatalf("备份不存在时现场文件被删了：%v", statErr)
	}
	if readLocalFile(t, target) != "现场唯一副本" {
		t.Fatal("现场内容被改动了")
	}
	if hasCommand(runner.commands(), "rm -rf") {
		t.Fatalf("备份缺失分支不得出现删除命令：%v", runner.commands())
	}
}

func TestActivatorRestoreMissingBackupIsSkipped(t *testing.T) {
	requireShell(t, "cp", "cmp")
	dir := t.TempDir()
	target := filepath.Join(dir, "core.json")
	backup := target + ".platform.task1.bak"
	writeFile(t, target, "平台清单", 0o644)
	activator := NewActivator()
	activator.Register(WriteEntry{Path: target, BackupPath: backup})
	outcomes, err := activator.Restore(context.Background(), &shellRunner{}, []WriteEntry{{Path: target}})
	if err != nil {
		t.Fatalf("备份缺失在还原语义下是 skipped，不是失败：%v", err)
	}
	if len(outcomes) != 1 || outcomes[0].State != "skipped" {
		t.Fatalf("幂等重放必须报 skipped：%+v", outcomes)
	}
}

func TestActivatorRestoreFilePreservesBytesAndMode(t *testing.T) {
	requireShell(t, "cp", "cmp")
	dir := t.TempDir()
	target := filepath.Join(dir, "core.json")
	backup := target + ".platform.task1.bak"
	writeFile(t, backup, "[{\"action\":\"copy\",\"path\":\"/sf/log\"}]\n", 0o644)
	writeFile(t, target, "平台清单", 0o600)
	activator := NewActivator()
	activator.Register(WriteEntry{Path: target, BackupPath: backup})
	outcomes, err := activator.Restore(context.Background(), &shellRunner{}, []WriteEntry{{Path: target}})
	if err != nil || len(outcomes) != 1 || outcomes[0].State != "restored" {
		t.Fatalf("还原应成功：%+v err=%v", outcomes, err)
	}
	if got := readLocalFile(t, target); got != "[{\"action\":\"copy\",\"path\":\"/sf/log\"}]\n" {
		t.Fatalf("还原后字节与原件不一致（尾换行被剥掉？）：%q", got)
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("cp -p 必须保留原件权限位，实际 %v", info.Mode().Perm())
	}
}

func TestActivatorRollbackDirTreeRestoresWholeTree(t *testing.T) {
	requireShell(t, "cp", "rm", "mv")
	dir := t.TempDir()
	target := filepath.Join(dir, "log_collect")
	backup := filepath.Join(dir, "backup", "log_collect")
	if err := os.MkdirAll(filepath.Join(backup, "tools"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(backup, "plugin"), "#!/bin/sh\necho orig\n", 0o755)
	writeFile(t, filepath.Join(backup, "tools", "manifest.json"), "{\"hash\":{}}", 0o644)
	if err := os.MkdirAll(filepath.Join(target, "tools"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(target, "plugin"), "平台覆盖版", 0o755)
	writeFile(t, filepath.Join(target, "tools", "extra.json"), "多出来的文件", 0o644)

	activator := NewActivator()
	activator.Register(WriteEntry{Path: target, IsDirTree: true, BackupPath: backup})
	if err := activator.Rollback(context.Background(), &shellRunner{}, []WriteEntry{{Path: target}}); err != nil {
		t.Fatalf("目录树回滚应成功：%v", err)
	}
	if got := readLocalFile(t, filepath.Join(target, "plugin")); got != "#!/bin/sh\necho orig\n" {
		t.Fatalf("入口脚本未被还原：%q", got)
	}
	if got := readLocalFile(t, filepath.Join(target, "tools", "manifest.json")); got != "{\"hash\":{}}" {
		t.Fatalf("子目录文件未被还原：%q", got)
	}
	if _, err := os.Stat(filepath.Join(target, "tools", "extra.json")); !os.IsNotExist(err) {
		t.Fatal("目录树回滚必须整树替换，平台多写的文件应消失")
	}
	if _, err := os.Stat(target + ".aside"); !os.IsNotExist(err) {
		t.Fatal(".aside 副本应在成功后清掉，不留垃圾")
	}
}

// TestActivatorDirTreeRollbackRefusesWithoutBackup 目录树版 B1：
// 没有备份时 rm -rf 绝不能执行——旧 rollback() 正是在这里删掉了现场插件目录。
func TestActivatorDirTreeRollbackRefusesWithoutBackup(t *testing.T) {
	requireShell(t, "cp", "rm", "mv")
	dir := t.TempDir()
	target := filepath.Join(dir, "log_collect")
	backup := filepath.Join(dir, "backup", "log_collect")
	if err := os.MkdirAll(target, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(target, "plugin"), "现场唯一副本", 0o755)
	runner := &shellRunner{}
	activator := NewActivator()
	activator.Register(WriteEntry{Path: target, IsDirTree: true, BackupPath: backup})
	err := activator.Rollback(context.Background(), runner, []WriteEntry{{Path: target}})
	if !errors.Is(err, ErrNoBackup) {
		t.Fatalf("备份缺失必须报错，实际 %v", err)
	}
	if got := readLocalFile(t, filepath.Join(target, "plugin")); got != "现场唯一副本" {
		t.Fatalf("现场插件目录被删/改了：%q", got)
	}
	// 脚本里出现 rm -rf 文本本身不算违规——它被 exit 21 守卫挡在后面。
	// 真正要钉死的是守卫顺序：无备份分支必须先终结，删除分支永远到不了。
	// （现场文件仍在已由上面的行为断言验证。）
	for _, cmd := range runner.commands() {
		guardIdx := strings.Index(cmd, "exit 21")
		rmIdx := strings.Index(cmd, "rm -rf")
		if rmIdx >= 0 && (guardIdx < 0 || guardIdx > rmIdx) {
			t.Fatalf("无备份守卫必须先于删除分支：%s", cmd)
		}
	}
}

func TestActivatorRegisterLastWinsAndEntriesSorted(t *testing.T) {
	activator := NewActivator()
	activator.Register(WriteEntry{Path: "/sf/b", BackupPath: "/sf/b.bak1"})
	activator.Register(WriteEntry{Path: "/sf/a", BackupPath: "/sf/a.bak"}, WriteEntry{Path: "/sf/b", BackupPath: "/sf/b.bak2"})
	activator.Register(WriteEntry{Path: ""}) // 空路径不登记
	entries := activator.Entries()
	if len(entries) != 2 || entries[0].Path != "/sf/a" || entries[1].BackupPath != "/sf/b.bak2" {
		t.Fatalf("登记应以最后一次为准且按路径排序：%+v", entries)
	}
	if _, ok := activator.Registered("/sf/none"); ok {
		t.Fatal("未登记路径不该被查到")
	}
	var nilActivator *Activator
	nilActivator.Register(WriteEntry{Path: "/sf/x"})
	if nilActivator.Entries() != nil {
		t.Fatal("nil Activator 应是空写入集")
	}
	if allowed, err := nilActivator.AssertRestorable([]WriteEntry{{Path: "/sf/x"}}); err != nil || len(allowed) != 0 {
		t.Fatalf("nil Activator 不该放行任何条目：%+v %v", allowed, err)
	}
}

func TestActivatorRequiresRunner(t *testing.T) {
	activator := NewActivator()
	activator.Register(WriteEntry{Path: "/sf/a", BackupPath: "/sf/a.bak"})
	if err := activator.Rollback(context.Background(), nil, []WriteEntry{{Path: "/sf/a"}}); err == nil {
		t.Fatal("缺执行器时必须报错而不是静默成功")
	}
	if _, err := activator.Restore(context.Background(), nil, []WriteEntry{{Path: "/sf/a"}}); err == nil {
		t.Fatal("缺执行器时必须报错")
	}
}

// TestStoreIntentSurvivesRestart 抗崩溃用例：进程重启（换成一个全新 Store 实例）
// 后仍能从盘上读回还原意图，凭它 + 自描述备份名收尾，而不是复活内存协程。
func TestStoreIntentSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	intent := RestoreIntent{
		Kind: KindSpcSlot, TaskKey: "Q2026111900001", WaitFor: WaitTaskTerminal,
		NodeIPs:   []string{"172.28.24.1", "172.28.25.1"},
		LocalIPs:  map[string]bool{"172.28.24.1": true},
		Dirs:      map[string]string{"172.28.24.1": "/sf/spc"},
		Slots:     []string{"core", "network"},
		CreatedAt: time.Now(),
	}
	if err := NewStore(dir).SaveIntent(intent); err != nil {
		t.Fatal(err)
	}
	// 模拟 bridge 重启：新进程、新 Store、内存里什么都没有。
	restarted := NewStore(dir)
	got, err := restarted.LoadIntents()
	if err != nil || len(got) != 1 {
		t.Fatalf("重启后必须能读回意图：%+v err=%v", got, err)
	}
	if got[0].TaskKey != intent.TaskKey || got[0].WaitFor != WaitTaskTerminal ||
		len(got[0].NodeIPs) != 2 || len(got[0].Slots) != 2 || !got[0].LocalIPs["172.28.24.1"] {
		t.Fatalf("意图内容丢失：%+v", got[0])
	}
	if err := restarted.DeleteIntent(KindSpcSlot, intent.TaskKey); err != nil {
		t.Fatal(err)
	}
	if err := restarted.DeleteIntent(KindSpcSlot, intent.TaskKey); err != nil {
		t.Fatalf("重复消费必须幂等：%v", err)
	}
	left, err := restarted.LoadIntents()
	if err != nil || len(left) != 0 {
		t.Fatalf("消费后不应再有意图：%+v err=%v", left, err)
	}
}

func TestStoreIntentsSortedAndIsolatedFromLedger(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(dir)
	older := RestoreIntent{Kind: KindSpcSlot, TaskKey: "task-older", CreatedAt: time.Now().Add(-time.Hour)}
	newer := RestoreIntent{Kind: KindSpcSlot, TaskKey: "task-newer", CreatedAt: time.Now()}
	if err := store.SaveIntent(newer); err != nil {
		t.Fatal(err)
	}
	if err := store.SaveIntent(older); err != nil {
		t.Fatal(err)
	}
	intents, err := store.LoadIntents()
	if err != nil || len(intents) != 2 || intents[0].TaskKey != "task-older" {
		t.Fatalf("意图应按创建时间升序（先写的先还原）：%+v err=%v", intents, err)
	}
	// 意图与账本形状不同：混在一起会让 List 解析出 Kind 为空的假账本条目。
	if err := store.Save(&AppliedState{Kind: KindSpcSlot, NodeIP: "172.28.24.1", DesiredHash: "abc", AppliedAt: time.Now()}); err != nil {
		t.Fatal(err)
	}
	ledger, err := store.List()
	if err != nil || len(ledger) != 1 || ledger[0].Kind != KindSpcSlot {
		t.Fatalf("账本列表被意图污染：%+v err=%v", ledger, err)
	}
}

func TestStoreIntentRejectsMissingKeyAndSkipsCorrupt(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(dir)
	if err := store.SaveIntent(RestoreIntent{Kind: KindSpcSlot}); err == nil {
		t.Fatal("缺 task_key 的意图必须拒绝落盘")
	}
	if err := store.SaveIntent(RestoreIntent{TaskKey: "t"}); err == nil {
		t.Fatal("缺 kind 的意图必须拒绝落盘")
	}
	if err := store.SaveIntent(RestoreIntent{Kind: KindSpcSlot, TaskKey: "good", CreatedAt: time.Now()}); err != nil {
		t.Fatal(err)
	}
	corrupt := filepath.Join(dir, "intents", "spc_slot-bad.json")
	writeFile(t, corrupt, "{不是 JSON", 0o600)
	intents, err := store.LoadIntents()
	if err != nil || len(intents) != 1 || intents[0].TaskKey != "good" {
		t.Fatalf("单条损坏只跳过该条，不该让其余任务没人收尾：%+v err=%v", intents, err)
	}
	var nilStore *Store
	if err := nilStore.SaveIntent(RestoreIntent{Kind: "k", TaskKey: "t"}); err != nil {
		t.Fatalf("nil Store 应安全：%v", err)
	}
	if got, err := nilStore.LoadIntents(); err != nil || got != nil {
		t.Fatalf("nil Store 应安全：%+v %v", got, err)
	}
}

// TestNormalizeRequiresBackupForRestorePolicy 把"无备份不删"前移到 descriptor 校验：
// 声明了依赖备份的策略却没给备份名，必须在开工前就被拒，而不是写到一半才发现。
func TestNormalizeRequiresBackupForRestorePolicy(t *testing.T) {
	base := DesiredState{
		Kind: KindSpcSlot, NodeIP: "172.28.24.1",
		Files: []FileSpec{{Path: "/sf/spc/core.json", Source: Source{Kind: SourceInline, Data: []byte("[]")}}},
		Policy: ReconcilePolicy{
			OnFailRollback:   RollbackSpec{Strategy: RollbackRestoreBackup},
			OnSuccessRestore: RestoreSpec{Strategy: RestoreOriginal, WaitFor: WaitTaskTerminal},
		},
	}
	if _, _, err := Normalize(base); err == nil || !strings.Contains(err.Error(), "TaskKey") {
		t.Fatalf("依赖备份的策略必须要求 TaskKey：%v", err)
	}
	base.TaskKey = "task-1"
	if _, _, err := Normalize(base); err == nil || !strings.Contains(err.Error(), "BackupPath") {
		t.Fatalf("依赖备份的策略必须要求 BackupPath：%v", err)
	}
	base.Files[0].BackupPath = "/sf/spc/core.json"
	if _, _, err := Normalize(base); err == nil || !strings.Contains(err.Error(), "相同") {
		t.Fatalf("备份与目标同路径必须拒绝：%v", err)
	}
	base.Files[0].BackupPath = "/sf/spc/core.json.platform.task-1.bak"
	if _, _, err := Normalize(base); err != nil {
		t.Fatalf("合法备份名应通过：%v", err)
	}
	base.Files[0].BackupPath = "../escape.bak"
	if _, _, err := Normalize(base); err == nil {
		t.Fatal("相对/穿越备份路径必须拒绝")
	}
}
