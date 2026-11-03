package main

// 本地中转副本生命周期的单测。
//
// 核心不变量只有一条：**任何被非终态作业引用的副本都不会被删除**。
// 其余规则（交付即删、失败 TTL、孤儿窗口、容量上限）都必须能被单独钉住，
// 否则一次误删就会让用户重拉 GB 级包，比"磁盘满"更难解释。

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func setupLocalCopyDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	localCopyDirOverride = dir
	t.Cleanup(func() { localCopyDirOverride = "" })
	return dir
}

func writeCopy(t *testing.T, dir, name string, size int, mod time.Time) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, make([]byte, size), 0o600); err != nil {
		t.Fatalf("写入测试副本失败: %v", err)
	}
	if err := os.Chtimes(path, mod, mod); err != nil {
		t.Fatalf("设置 mtime 失败: %v", err)
	}
	return path
}

func mustExist(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("文件应保留但已消失: %s (%v)", path, err)
	}
}

func mustNotExist(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("文件应被删除但仍然存在: %s", path)
	}
}

func TestDiscardDeliveredCopyRemovesFile(t *testing.T) {
	dir := setupLocalCopyDir(t)
	path := writeCopy(t, dir, "delivered.zip", 64, time.Now())
	runner := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-delivered", LocalPath: path, Phase: phaseCompleting}

	runner.discardDeliveredCopy(job)

	mustNotExist(t, path)
	if job.LocalDeletedAt == "" {
		t.Fatal("删除后必须记录 LocalDeletedAt，否则无法区分'已清理'与'从未写下'")
	}
}

func TestDiscardDeliveredCopyHonorsDisabledSwitch(t *testing.T) {
	t.Setenv("HCI_BRIDGE_KEEP_LOCAL_DAYS", "-1")
	dir := setupLocalCopyDir(t)
	path := writeCopy(t, dir, "kept.zip", 64, time.Now())
	runner := newTestDeliveryRunner(t)

	runner.discardDeliveredCopy(&deliveryJob{JobID: "dj-kept", LocalPath: path})

	mustExist(t, path)
}

func TestReconcileClassifiesCopies(t *testing.T) {
	dir := setupLocalCopyDir(t)
	now := time.Now()

	done := writeCopy(t, dir, "done.zip", 32, now)
	// 非终态的文件即使很旧也不许删：它可能正在续传。
	running := writeCopy(t, dir, "running.zip", 32, now.Add(-3*time.Hour))
	failed := writeCopy(t, dir, "failed.zip", 32, now.Add(-1*time.Hour))
	orphanOld := writeCopy(t, dir, "orphan-old.zip", 32, now.Add(-2*time.Hour))
	orphanFresh := writeCopy(t, dir, "orphan-fresh.zip", 32, now.Add(-10*time.Minute))

	runner := newTestDeliveryRunner(t)
	runner.jobs["dj-done"] = &deliveryJob{JobID: "dj-done", LocalPath: done, Phase: phaseDone}
	runner.jobs["dj-running"] = &deliveryJob{JobID: "dj-running", LocalPath: running, Phase: phaseUploading}
	runner.jobs["dj-failed"] = &deliveryJob{JobID: "dj-failed", LocalPath: failed, Phase: phaseFailed}

	report := runner.reconcileLocalCopies(now)

	mustNotExist(t, done)      // 已交付：副本无读者
	mustNotExist(t, orphanOld) // 无引用且超过保护窗口
	mustExist(t, running)      // 非终态：绝不删
	mustExist(t, failed)       // 失败终态：TTL 内保留，供重试续传
	mustExist(t, orphanFresh)  // 保护窗口内：可能是刚写下但 job 尚未落盘
	if report.Removed != 2 {
		t.Fatalf("应删除 2 个文件，实际 %d（%+v）", report.Removed, report.Reasons)
	}
	if report.Reasons["delivered"] != 1 || report.Reasons["orphan"] != 1 {
		t.Fatalf("删除理由统计错误: %+v", report.Reasons)
	}
}

func TestReconcileRemovesExpiredFailedCopy(t *testing.T) {
	t.Setenv("HCI_BRIDGE_KEEP_LOCAL_DAYS", "1")
	dir := setupLocalCopyDir(t)
	now := time.Now()
	path := writeCopy(t, dir, "failed.zip", 32, now.Add(-48*time.Hour))

	runner := newTestDeliveryRunner(t)
	runner.jobs["dj-failed"] = &deliveryJob{JobID: "dj-failed", LocalPath: path, Phase: phaseFailed}

	report := runner.reconcileLocalCopies(now)

	mustNotExist(t, path)
	if report.Reasons["ttl"] != 1 {
		t.Fatalf("应以 ttl 理由删除: %+v", report.Reasons)
	}
}

func TestReconcileEvictsOldestWhenOverCap(t *testing.T) {
	t.Setenv("HCI_BRIDGE_LOCAL_MAX_MB", "1")
	dir := setupLocalCopyDir(t)
	now := time.Now()
	// 两份 700 KiB 孤儿（未过 1h 保护窗口），总量 1.37 MiB 超过 1 MiB 上限。
	older := writeCopy(t, dir, "older.zip", 700<<10, now.Add(-30*time.Minute))
	newer := writeCopy(t, dir, "newer.zip", 700<<10, now.Add(-20*time.Minute))

	runner := newTestDeliveryRunner(t)
	report := runner.reconcileLocalCopies(now)

	mustNotExist(t, older)
	mustExist(t, newer)
	if report.Reasons["over_cap"] != 1 {
		t.Fatalf("应以 over_cap 理由淘汰最旧副本: %+v", report.Reasons)
	}
}

func TestReconcileDryRunKeepsEverything(t *testing.T) {
	t.Setenv("HCI_BRIDGE_CLEANUP_DRY_RUN", "1")
	dir := setupLocalCopyDir(t)
	now := time.Now()
	done := writeCopy(t, dir, "done.zip", 32, now)
	orphan := writeCopy(t, dir, "orphan.zip", 32, now.Add(-2*time.Hour))

	runner := newTestDeliveryRunner(t)
	runner.jobs["dj-done"] = &deliveryJob{JobID: "dj-done", LocalPath: done, Phase: phaseDone}

	report := runner.reconcileLocalCopies(now)

	mustExist(t, done)
	mustExist(t, orphan)
	if !report.DryRun || report.Removed != 2 {
		t.Fatalf("dry-run 应只统计不删除: removed=%d dry_run=%v", report.Removed, report.DryRun)
	}
}

func TestReconcileDisabledKeepsEverything(t *testing.T) {
	t.Setenv("HCI_BRIDGE_KEEP_LOCAL_DAYS", "-1")
	dir := setupLocalCopyDir(t)
	now := time.Now()
	done := writeCopy(t, dir, "done.zip", 32, now)
	orphan := writeCopy(t, dir, "orphan.zip", 32, now.Add(-2*time.Hour))

	runner := newTestDeliveryRunner(t)
	runner.jobs["dj-done"] = &deliveryJob{JobID: "dj-done", LocalPath: done, Phase: phaseDone}

	if report := runner.reconcileLocalCopies(now); report.Removed != 0 {
		t.Fatalf("禁用清理时不得删除任何文件: %+v", report)
	}
	mustExist(t, done)
	mustExist(t, orphan)
}
