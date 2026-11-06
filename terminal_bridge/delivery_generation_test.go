package main

// 交付代际与协作式取消的回归护栏。
//
// 2026-11-06 Q2026110601054：旧作业 15:51:11 已收到 "context canceled"，
// 却因 runJob 把它当成普通可恢复错误写成 retrying，15:55:10 又复活继续
// collect_finished → 拉包 → 落 fatal 并写回平台任务。取消只是"通知"，
// 不是"资格"；这里把资格判据与取消入口分别钉住。

import (
	"context"
	"errors"
	"testing"
)

func TestSubmitMarksStaleJobSuperseded(t *testing.T) {
	r := newTestDeliveryRunner(t)
	old := &deliveryJob{
		JobID: "dj-task-1", TaskID: "task-1", Phase: phaseUploading,
		SubmittedSize: 100, UploadToken: "old-token",
	}
	r.jobs[old.JobID] = old

	fresh := &deliveryJob{
		JobID: "dj-task-1", TaskID: "task-1", Phase: phaseWaitingCollect,
		SubmittedSize: 200, UploadToken: "new-token",
	}
	if got := r.submit(fresh); got != fresh {
		t.Fatal("新会话必须替换旧作业，而不是复用旧基线")
	}
	if !old.Superseded {
		t.Fatal("旧作业必须被标记 superseded，否则还可能被重排复活")
	}
	if old.JobID != "dj-task-1-superseded" {
		t.Fatalf("旧作业应改用独立 ID 落盘，实际 %s", old.JobID)
	}
	old.LastErrorCode = "LC-TRANSFER-004"
	if view := toDeliveryJobView(old); view.ErrorCode != "" {
		t.Fatal("被顶替作业的过期错误码不得回传前端，否则会污染新会话状态")
	}
}

func TestAdvanceHonorsCanceledContext(t *testing.T) {
	r := newTestDeliveryRunner(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	job := &deliveryJob{JobID: "dj-task-2", TaskID: "task-2", Phase: phaseUploading}

	if err := r.advance(ctx, job); !errors.Is(err, context.Canceled) {
		t.Fatalf("阶段入口必须检查 ctx.Err()，实际 %v", err)
	}
}

func TestRequeueEligibleRejectsSupersededAndStaleMembership(t *testing.T) {
	r := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-task-3", TaskID: "task-3", Phase: phaseRetrying}
	r.jobs[job.JobID] = job
	if !r.requeueEligible(job) {
		t.Fatal("正常 retrying 作业应可重排")
	}

	job.Superseded = true
	if r.requeueEligible(job) {
		t.Fatal("被顶替的作业不得重排")
	}

	job.Superseded = false
	r.jobs[job.JobID] = &deliveryJob{JobID: "dj-task-3", Phase: phaseRetrying}
	if r.requeueEligible(job) {
		t.Fatal("已从活跃集合移除的旧指针不得重排（membership 检查）")
	}
}

func TestRunJobSkipsSupersededJob(t *testing.T) {
	r := newTestDeliveryRunner(t)
	job := &deliveryJob{
		JobID: "dj-task-4", TaskID: "task-4", Phase: phaseUploading, Superseded: true,
	}
	r.runJob(job)
	if job.Phase != phaseUploading {
		t.Fatalf("被顶替作业不得推进任何阶段，实际 %s", job.Phase)
	}
}
