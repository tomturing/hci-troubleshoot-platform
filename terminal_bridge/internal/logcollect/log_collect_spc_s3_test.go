package logcollect

// S3 消费者迁移验收：spc 编排委托 reconcile 骨架后，产品语义的关键不变量
// 必须仍然成立——写前清残留 tmp、写入失败不留半截文件、B1 门 fail-closed、
// 持久还原意图的落盘/ supersede / 重启消费。骨架自身的不变量（written-set、
// 互斥、负超时）在 reconcile 包内验收，这里只验消费者接线。

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"terminal_bridge/internal/shared/logcontract"
	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/sshx/session"
	"testing"
	"time"
)

// TestBuildSpcPrepareCommandCleansStaleTmp argv 写入协议用确定性 <target>.tmp，
// 断链/进程被杀会留下半截 tmp：prepare 必须在备份之前清掉它，
// 否则残留永久占盘并在排查时误导"是不是写了一半"。
func TestBuildSpcPrepareCommandCleansStaleTmp(t *testing.T) {
	command := buildSpcPrepareCommand("/sf/x/spc/core.json", "/sf/x/spc/core.json.platform.task1.bak")
	clean := strings.Index(command, "rm -f '/sf/x/spc/core.json.tmp'")
	if clean < 0 {
		t.Fatalf("缺少确定性 tmp 的崩溃残留清理：%s", command)
	}
	ensureDir := strings.Index(command, "mkdir -p")
	selfHeal := strings.Index(command, "__prev")
	if clean < ensureDir || clean > selfHeal {
		t.Fatalf("清 tmp 必须在目录重建之后、写前自愈之前：%s", command)
	}
}

// TestSpcWriteFailureLeavesNoHalfFile 真 shell 验收：目录不可写时写入失败，
// 目标文件必须保持原件（备份回滚），且不得留下半截 tmp。
// root 下 chmod 不生效，跳过；CI 与开发机均非 root。
func TestSpcWriteFailureLeavesNoHalfFile(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root 下目录权限不生效，跳过")
	}
	if _, err := exec.LookPath("cmp"); err != nil {
		t.Skip("环境缺 cmp，跳过真 shell 回归")
	}
	dir := t.TempDir()
	target := filepath.Join(dir, "core.json")
	backup := target + ".platform.task1.bak"
	original := "[]\n"
	if err := os.WriteFile(target, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backup, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o555); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(dir, 0o755) // 恢复权限，否则 t.TempDir 清理失败

	content := `[{"action":"copy","path":"/var/log/a"}]`
	command := buildSpcWriteVerifyCommand(target, backup, content)
	out, _ := exec.Command("sh", "-c", command).CombinedOutput()
	if !spcMarkerHas(string(out), "SPC_WRITE_FAIL") {
		t.Fatalf("只读目录里写入必须报失败：%s", out)
	}
	onDisk, err := os.ReadFile(target)
	if err != nil || string(onDisk) != original {
		t.Fatalf("写入失败后目标必须保持原件：%q err=%v", onDisk, err)
	}
	if _, err := os.Stat(target + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("失败路径不得留下半截 tmp：err=%v", err)
	}
}

// TestRollbackSpcAllGateRefusesWithoutRegistration B1 门 fail-closed：
// 登记了写入但备份名为空（备份归属不明）时一条命令都不发，逐台报还原失败。
// session 传 nil：任何命令路径被走到都会 panic，测试通过即证明零命令。
func TestRollbackSpcAllGateRefusesWithoutRegistration(t *testing.T) {
	target, err := spcFilePath(SpcDeployDir, "core")
	if err != nil {
		t.Fatal(err)
	}
	activator := reconcile.NewActivator()
	// 写成功但备份名丢失：回滚前提不满足，绝不能凭猜测碰现场。
	activator.Register(reconcile.WriteEntry{Path: target, BackupPath: ""})
	st := &spcTaskState{
		TaskID: "task-gate", Slot: "core",
		Targets:      []string{"172.28.24.1"},
		activator:    activator,
		restoreState: "pending",
	}
	b := session.NewHub()
	restored, failed := rollbackSpcAll(b, nil, st, "写入未通过，已回滚", []string{"172.28.24.1"})
	if restored != 0 || failed != 1 {
		t.Fatalf("门不过必须零恢复、全失败：%d %d", restored, failed)
	}
	snapshot := st.snapshot()
	if snapshot.RestoreState != "failed" {
		t.Fatalf("fail-closed 必须显式报还原失败：%+v", snapshot)
	}
	for _, host := range st.hostsCopy() {
		if host.RestoreState != "failed" || host.ErrorDetail == "" {
			t.Fatalf("逐台必须留下拒绝原因：%+v", host)
		}
	}
}

// TestRollbackSpcAllEmptyWrittenSetSendsNothing 门通过但写入集为空：
// RollbackWritten 一条命令都不发（从未写入的节点没有需要撤销的改动）。
// session 同样传 nil，证明空集路径不触达任何 SSH 动作。
func TestRollbackSpcAllEmptyWrittenSetSendsNothing(t *testing.T) {
	target, err := spcFilePath(SpcDeployDir, "core")
	if err != nil {
		t.Fatal(err)
	}
	backup, err := spcBackupPath(target, "task-empty")
	if err != nil {
		t.Fatal(err)
	}
	activator := reconcile.NewActivator()
	activator.Register(reconcile.WriteEntry{Path: target, BackupPath: backup})
	st := &spcTaskState{
		TaskID: "task-empty", Slot: "core",
		Targets:      []string{"172.28.24.1"},
		activator:    activator,
		restoreState: "pending",
		// written 零值 = 空写入集
	}
	b := session.NewHub()
	restored, failed := rollbackSpcAll(b, nil, st, "写入未通过，已回滚", []string{"172.28.24.1"})
	if restored != 0 || failed != 0 {
		t.Fatalf("空写入集必须零命令零失败：%d %d", restored, failed)
	}
	if st.snapshot().RestoreState != "success" {
		t.Fatalf("无可回滚项应视为收尾完成：%+v", st.snapshot())
	}
}

// TestSpcHostFromResultBusyAndOverlay 骨架结论 → 逐台证据的还原：
// busy 没有逐槽位明细（动作未执行）；动作已返回但父 ctx 取消时
// 骨架叠加错误码，必须把 Written 压回 false，否则回执与写入集对不上。
func TestSpcHostFromResultBusyAndOverlay(t *testing.T) {
	busy := spcHostFromResult(reconcile.HostResult{Host: "172.28.24.1", ErrorCode: reconcile.CodeBusy})
	if busy.ErrorCode != spcErrBusy || busy.Written || busy.ErrorDetail == "" {
		t.Fatalf("busy 必须映射 host_busy 且不可标已写入：%+v", busy)
	}
	if logcontract.ValidationErrorCode(busy.ErrorCode) != logcontract.ErrSpcHostBusy {
		t.Fatalf("host_busy 必须映射 LC-SPC-011：%s", busy.ErrorCode)
	}
	overlay := spcHostFromResult(reconcile.HostResult{
		Host: "172.28.24.1", ErrorCode: "host_timeout", ErrorDetail: "ctx canceled",
		Value: spcHostState{IP: "172.28.24.1", Written: true},
	})
	if overlay.Written || overlay.ErrorCode != "host_timeout" {
		t.Fatalf("骨架叠加错误时必须压回未写入：%+v", overlay)
	}
}

// TestSpcRestoreIntentLifecycle 持久意图全生命周期（§3.2 抗崩溃第四件）：
// 落盘可重启读回 → supersede 只删主机×槽位相交的旧意图 →
// 重启消费时空意图直接清理、内存 watcher 活着的任务不双头接管。
//
// 注意：reconcile 包级账本用 sync.Once 固定目录，整个主包测试进程里
// 只有这里触达它，因此 t.Setenv 一次即可；各阶段共用同一临时目录。
func TestSpcRestoreIntentLifecycle(t *testing.T) {
	t.Setenv("HCI_BRIDGE_LOG_DIR", t.TempDir())
	b := session.NewHub()

	// ── 落盘与读回：只登记真正写成功的节点 ──────────────────────────
	st := &spcTaskState{
		TaskID: "task-a", Slot: "core",
		LocalIPs: map[string]bool{"172.28.24.1": true},
	}
	st.setHosts([]spcHostState{
		{IP: "172.28.24.1", Written: true, Dir: SpcDeployDir},
		{IP: "172.28.25.1", Written: false}, // 未写入节点不得进入还原范围
	})
	persistSpcRestoreIntent(b, st, "trace", "case", "172.28.24.1", "")
	intent := findSpcRestoreIntent("task-a")
	if intent == nil {
		t.Fatal("意图必须落盘且可按任务键读回（模拟重启后内存现场丢失）")
	}
	if intent.Kind != reconcile.KindSpcSlot || intent.WaitFor != reconcile.WaitTaskTerminal {
		t.Fatalf("意图形状不对：%+v", intent)
	}
	if len(intent.NodeIPs) != 1 || intent.NodeIPs[0] != "172.28.24.1" {
		t.Fatalf("还原范围必须只含已写入节点：%+v", intent.NodeIPs)
	}
	if intent.Dirs["172.28.24.1"] != SpcDeployDir || !intent.LocalIPs["172.28.24.1"] {
		t.Fatalf("目录与本机地址必须随意图落盘：%+v", intent)
	}

	// ── supersede：主机×槽位都相交才删，单维相交保留 ────────────────
	mustSave := func(taskKey string, ips, slots []string) {
		t.Helper()
		if err := reconcile.SaveIntent(reconcile.RestoreIntent{
			Kind: reconcile.KindSpcSlot, TaskKey: taskKey, WaitFor: reconcile.WaitTaskTerminal,
			NodeIPs: ips, Slots: slots, CreatedAt: time.Now(), Reason: "custom_scenario",
		}); err != nil {
			t.Fatal(err)
		}
	}
	mustSave("task-old", []string{"172.28.24.1"}, []string{"core"})   // 双相交 → 删
	mustSave("task-other", []string{"172.28.99.1"}, []string{"core"}) // 主机不相交 → 留
	mustSave("task-slot", []string{"172.28.24.1"}, []string{"net"})   // 槽位不相交 → 留
	current := reconcile.RestoreIntent{
		Kind: reconcile.KindSpcSlot, TaskKey: "task-b",
		NodeIPs: []string{"172.28.24.1"}, Slots: []string{"core"},
	}
	// task-a 与 task-old 都与 task-b 双相交：写前自愈已转移所有权，
	// 旧意图留着会让重启恢复把旧原件盖到新任务清单上。
	if deleted := supersedeSpcRestoreIntents(current); deleted != 2 {
		t.Fatalf("应删除 task-a 与 task-old 两条相交意图，实际 %d", deleted)
	}
	if findSpcRestoreIntent("task-a") != nil || findSpcRestoreIntent("task-old") != nil {
		t.Fatal("相交旧意图必须被删除")
	}
	if findSpcRestoreIntent("task-other") == nil || findSpcRestoreIntent("task-slot") == nil {
		t.Fatal("单维不相交的意图必须保留")
	}
	// 后续阶段用 nil session 验证重启消费：先把不相交意图清掉，
	// 否则 recover 会拿 nil session 去还原它们（那不是本阶段要验的路径）。
	if err := reconcile.DeleteIntent(reconcile.KindSpcSlot, "task-other"); err != nil {
		t.Fatal(err)
	}
	if err := reconcile.DeleteIntent(reconcile.KindSpcSlot, "task-slot"); err != nil {
		t.Fatal(err)
	}

	// ── 重启消费：空意图直接清理（无需会话）─────────────────────────
	mustSave("task-empty", nil, []string{"core"})
	RecoverSpcRestoreIntents(b, nil, "trace", "case", "172.28.24.1", "")
	if findSpcRestoreIntent("task-empty") != nil {
		t.Fatal("无还原范围的意图必须直接清理")
	}

	// ── 重启消费：内存 watcher 活着的任务不双头接管 ─────────────────
	spcTaskStore.put(&spcTaskState{TaskID: "task-live", CreatedAt: time.Now()})
	defer spcTaskStore.reset()
	mustSave("task-live", []string{"172.28.24.1"}, []string{"core"})
	RecoverSpcRestoreIntents(b, nil, "trace", "case", "172.28.24.1", "")
	if findSpcRestoreIntent("task-live") == nil {
		t.Fatal("watcher 活着时意图必须保留（还原责任归 watcher，不双头接管）")
	}
}
