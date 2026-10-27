package main

// 交付作业的状态与落盘单测。
//
// 守护的不变量：
//  1. 落盘是原子的（tmp+rename），不会留下半截 JSON；
//  2. 恢复只挑非终态 job，终态不会重复执行；
//  3. 已传分片登记幂等（并发窗口完成顺序不确定）；
//  4. 并发度在任何探测失败下都回落到 1，绝不因为"猜错了"把客户机拖垮。

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDeliveryStoreRoundTrip(t *testing.T) {
	dir := t.TempDir()
	store := newDeliveryStore(dir)
	job := &deliveryJob{
		JobID: "dj-T1", TaskID: "T1", CaseID: "Q1", NodeIP: "10.0.0.1",
		Phase: phasePulling, SourceSize: 100, PulledBytes: 40,
	}
	if err := store.save(job); err != nil {
		t.Fatalf("落盘失败: %v", err)
	}
	loaded := store.load("dj-T1")
	if loaded == nil {
		t.Fatal("读取失败")
	}
	if loaded.PulledBytes != 40 || loaded.Phase != phasePulling {
		t.Fatalf("字段丢失: %+v", loaded)
	}
	// tmp 文件必须已被 rename 掉，否则恢复时可能读到半截内容
	if _, err := os.Stat(filepath.Join(dir, "delivery-jobs", "dj-T1.json.tmp")); err == nil {
		t.Fatal("残留 tmp 文件")
	}
}

func TestDeliveryStoreListSkipsTerminal(t *testing.T) {
	store := newDeliveryStore(t.TempDir())
	_ = store.save(&deliveryJob{JobID: "dj-run", Phase: phaseUploading})
	_ = store.save(&deliveryJob{JobID: "dj-done", Phase: phaseDone})
	_ = store.save(&deliveryJob{JobID: "dj-failed", Phase: phaseFailed})

	jobs := store.list()
	if len(jobs) != 1 || jobs[0].JobID != "dj-run" {
		t.Fatalf("只应恢复非终态作业，实际 %d 个", len(jobs))
	}
}

// 落盘目录不可用（cluster 模式未挂卷）时不能 panic，退化为纯内存。
func TestDeliveryStoreDisabledWithoutDir(t *testing.T) {
	store := newDeliveryStore("")
	if store.enabled() {
		t.Fatal("空目录不应认为可用")
	}
	if err := store.save(&deliveryJob{JobID: "x"}); err != nil {
		t.Fatalf("禁用态保存应静默成功: %v", err)
	}
	if store.load("x") != nil {
		t.Fatal("禁用态不应能读到")
	}
}

func TestDeliveryProgress(t *testing.T) {
	cases := []struct {
		job  deliveryJob
		want int
	}{
		{deliveryJob{Phase: phaseWaitingCollect}, 0},
		{deliveryJob{Phase: phasePulling, SourceSize: 100, PulledBytes: 50}, 25},
		{deliveryJob{Phase: phasePulling, SourceSize: 100, PulledBytes: 100}, 50},
		{deliveryJob{Phase: phaseUploading, PartCount: 10}, 50},
		{deliveryJob{Phase: phaseUploading, PartCount: 10, UploadedParts: []int{1, 2, 3, 4, 5}}, 75},
		{deliveryJob{Phase: phaseDone}, 100},
	}
	for _, tc := range cases {
		if got := tc.job.progress(); got != tc.want {
			t.Fatalf("phase=%s 进度期望 %d，实际 %d", tc.job.Phase, tc.want, got)
		}
	}
}

func TestAddUploadedPartDedupes(t *testing.T) {
	job := &deliveryJob{}
	job.addUploadedPart(3)
	job.addUploadedPart(1)
	job.addUploadedPart(3)
	if len(job.UploadedParts) != 2 {
		t.Fatalf("重复登记未去重: %v", job.UploadedParts)
	}
	if job.UploadedParts[0] != 1 || job.UploadedParts[1] != 3 {
		t.Fatalf("分片应升序: %v", job.UploadedParts)
	}
}

// 显式覆盖优先于硬件探测：现网特殊设备靠它兜底。
func TestDecideDeliveryConcurrencyRespectsEnv(t *testing.T) {
	t.Setenv("HCI_BRIDGE_DELIVERY_CONCURRENCY", "3")
	t.Setenv("HCI_BRIDGE_DELIVERY_TASKS", "2")
	decision := decideDeliveryConcurrency()
	if decision.Parts != 3 || decision.Tasks != 2 {
		t.Fatalf("环境变量未生效: %+v", decision)
	}
	if decision.Reason != "env:HCI_BRIDGE_DELIVERY_CONCURRENCY" {
		t.Fatalf("reason 应记录来源: %s", decision.Reason)
	}
}

// 越界的覆盖值必须被夹紧，不能因为写错环境变量开出 64 路并发。
func TestDecideDeliveryConcurrencyClamps(t *testing.T) {
	t.Setenv("HCI_BRIDGE_DELIVERY_CONCURRENCY", "64")
	decision := decideDeliveryConcurrency()
	if decision.Parts != maxDeliveryParts {
		t.Fatalf("应夹到上限 %d，实际 %d", maxDeliveryParts, decision.Parts)
	}
}

// 大分片必须按内存预算压低并发：64MB × 4 在 4GB 机器上会直接触发换页。
func TestChunkConcurrencyForMemoryBudget(t *testing.T) {
	decision := deliveryConcurrency{Parts: 4, Tasks: 1}
	if got := chunkConcurrencyFor(decision, 64*1024*1024, 512); got != 1 {
		t.Fatalf("小内存大分片应压到 1，实际 %d", got)
	}
	if got := chunkConcurrencyFor(decision, 4*1024*1024, 8192); got != 4 {
		t.Fatalf("大内存小分片应允许 4，实际 %d", got)
	}
}

// 交付模式判定：只有显式声明 direct 才跳过"等采集完成"。
//
// 这条开关决定「acli 已采集完成 → 直接上传」能否成立：
// collect 会进 waitCollect，其中的"包早于本次作业创建即判为上一次任务的产物"
// （容差 5 分钟）会把补交付必然误杀；而空值/未知值必须回落到 collect，
// 否则会在采集还在写包时把半截 zip 搬走。
func TestDeliveryModeIsDirect(t *testing.T) {
	direct := []string{"direct", "Direct", " DIRECT ", "direct\t"}
	for _, mode := range direct {
		if !deliveryModeIsDirect(mode) {
			t.Fatalf("%q 应判定为补交付", mode)
		}
	}
	for _, mode := range []string{"", "collect", "DIRECTED", "diret"} {
		if deliveryModeIsDirect(mode) {
			t.Fatalf("%q 不得判定为补交付（必须回落到 collect）", mode)
		}
	}
}
