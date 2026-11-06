package main

import (
	"testing"
	"time"
)

func TestStallTrackerTerminatesAfterTimeoutWithoutProgress(t *testing.T) {
	tracker := newStallTracker(30 * time.Minute)
	start := time.Unix(0, 0)
	signature := "uploading|parts=19|pulled=123"
	if tracker.observe(start, signature, true) {
		t.Fatal("第一次观察不能判停摆")
	}
	if tracker.observe(start.Add(29*time.Minute), signature, true) {
		t.Fatal("未到阈值不能判停摆")
	}
	if !tracker.observe(start.Add(30*time.Minute), signature, true) {
		t.Fatal("连续 30 分钟无进展必须判停摆")
	}
}

func TestStallTrackerResetsOnProgress(t *testing.T) {
	tracker := newStallTracker(30 * time.Minute)
	start := time.Unix(0, 0)
	tracker.observe(start, "uploading|parts=19", true)
	// 第 20 片传完：签名变化，计时必须从头开始。
	if tracker.observe(start.Add(29*time.Minute), "uploading|parts=20", true) {
		t.Fatal("有进展不能判停摆")
	}
	if tracker.observe(start.Add(58*time.Minute), "uploading|parts=20", true) {
		t.Fatal("距上次进展只有 29 分钟，不能判停摆")
	}
	if !tracker.observe(start.Add(59*time.Minute), "uploading|parts=20", true) {
		t.Fatal("距上次进展满 30 分钟必须判停摆")
	}
}

// 等采集（waiting_collect）与等平台合并（completing）阶段字节天然不动，
// 却各有自己的上限，绝不能按停摆砍——否则一个正常等节点采 40 分钟的作业会被误杀。
func TestStallTrackerIgnoresUnwatchedPhases(t *testing.T) {
	tracker := newStallTracker(30 * time.Minute)
	start := time.Unix(0, 0)
	tracker.observe(start, "waiting_collect", false)
	if tracker.observe(start.Add(2*time.Hour), "waiting_collect", false) {
		t.Fatal("不可观测阶段不得判停摆")
	}
	// 回到上传阶段后重新计时：不能把等采集的时长算进来。
	tracker.observe(start.Add(2*time.Hour), "uploading|parts=0", true)
	if tracker.observe(start.Add(2*time.Hour+29*time.Minute), "uploading|parts=0", true) {
		t.Fatal("上传阶段刚开始不到 30 分钟，不能判停摆")
	}
}

func TestProgressSignatureWatchesOnlyTransferPhases(t *testing.T) {
	r := &deliveryRunner{jobs: map[string]*deliveryJob{}}
	job := &deliveryJob{JobID: "j1", Phase: phaseUploading, PulledBytes: 100}
	sig, watched := r.progressSignature(job)
	if !watched {
		t.Fatal("上传阶段应参与停摆判定")
	}
	job.addUploadedPart(1)
	next, _ := r.progressSignature(job)
	if next == sig {
		t.Fatal("新增分片后签名必须变化")
	}

	job.Phase = phaseWaitingCollect
	if _, watched = r.progressSignature(job); watched {
		t.Fatal("等采集阶段不应参与停摆判定")
	}
	job.Phase = phaseCompleting
	if _, watched = r.progressSignature(job); watched {
		t.Fatal("收尾阶段不应参与停摆判定")
	}
}
