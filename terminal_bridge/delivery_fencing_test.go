package main

// 僵尸作业的副作用护栏（fencing）。
//
// 2026-11-06 Q2026110601054 / Q2026110600112：旧作业收到取消之后仍会
// 继续拉包、发 PUT、甚至收尾；而本地副本与 journal 文件名都按 task_id 命名，
// 新旧两代作业指向**同一份文件**。这里的用例把"资格丧失即停止一切副作用"
// 钉成契约，而不是依赖"取消信号一定会到"。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"
)

func TestFenceAllowsUnmanagedJob(t *testing.T) {
	r := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-unmanaged", TaskID: "unmanaged"}
	if err := r.fence(context.Background(), job); err != nil {
		t.Fatalf("未受管作业应放行（既有直调路径），实际 %v", err)
	}
}

func TestFenceRejectsSupersededJob(t *testing.T) {
	r := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-s1", TaskID: "s1", Superseded: true}
	r.jobs[job.JobID] = job

	if err := r.fence(context.Background(), job); !errors.Is(err, errDeliverySuperseded) {
		t.Fatalf("被顶替作业必须被闸门拒绝，实际 %v", err)
	}
}

func TestFenceRejectsSlotTakenByNewSession(t *testing.T) {
	r := newTestDeliveryRunner(t)
	stale := &deliveryJob{JobID: "dj-s2", TaskID: "s2"}
	fresh := &deliveryJob{JobID: "dj-s2", TaskID: "s2"}
	// submit 顶替后的真实形态：键上已是新指针，旧指针手上还留着 Superseded=false 的副本。
	r.jobs[stale.JobID] = fresh

	if err := r.fence(context.Background(), stale); !errors.Is(err, errDeliverySuperseded) {
		t.Fatalf("执行位被新会话占用后旧作业必须被拒绝，实际 %v", err)
	}
	if err := r.fence(context.Background(), fresh); err != nil {
		t.Fatalf("当前执行者必须放行，实际 %v", err)
	}
}

func TestFencePropagatesContextCancel(t *testing.T) {
	r := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-s3", TaskID: "s3"}
	r.jobs[job.JobID] = job
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if err := r.fence(ctx, job); !errors.Is(err, context.Canceled) {
		t.Fatalf("取消必须原样传出（不能被伪装成 superseded），实际 %v", err)
	}
}

// TestSaveJobDoesNotOverwriteNewSessionJournal 是"僵尸覆盖 journal"的回归。
// journal 文件名是 dj-<task>.json，两代作业同名；旧作业收尾若无条件 save，
// 会把新作业刚写的分片数覆盖回旧值，下一轮续传从第一片重来。
func TestSaveJobDoesNotOverwriteNewSessionJournal(t *testing.T) {
	r := newTestDeliveryRunner(t)
	taskID := "task-journal"
	fresh := &deliveryJob{JobID: "dj-" + taskID, TaskID: taskID, Phase: phaseUploading}
	fresh.addUploadedPart(1)
	fresh.addUploadedPart(2)
	fresh.addUploadedPart(3)
	r.jobs[fresh.JobID] = fresh
	if err := r.saveJob(fresh); err != nil {
		t.Fatalf("当前执行者应能落盘: %v", err)
	}

	stale := &deliveryJob{JobID: "dj-" + taskID, TaskID: taskID, Phase: phaseWaitingCollect, Superseded: true}
	if err := r.saveJob(stale); err != nil {
		t.Fatalf("跳过落盘不应报错: %v", err)
	}

	raw, err := os.ReadFile(r.store.path(fresh.JobID))
	if err != nil {
		t.Fatalf("读取 journal 失败: %v", err)
	}
	var saved deliveryJob
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatalf("journal 不是合法 JSON: %v", err)
	}
	if saved.Phase != phaseUploading {
		t.Fatalf("journal 阶段被旧作业覆盖为 %s", saved.Phase)
	}
	if len(saved.UploadedParts) != 3 {
		t.Fatalf("journal 分片数被旧作业覆盖为 %d，期望 3", len(saved.UploadedParts))
	}
}

// TestUploadOnePartFencedBeforePut 复核"一片都不许发"。
// 闸门必须在读盘分配缓冲之前就生效：8 MiB 的片会在旧作业上白占内存，
// 而它占的内存正是新作业起不了 worker 的原因。
func TestUploadOnePartFencedBeforePut(t *testing.T) {
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	job := prepareJob(t, srv.URL, "task-fence-put", "tok", 128, 64)
	job.Pace = uploadPace{Parts: 1, PartTimeout: 1, Reason: "test"}
	client := platformHTTPClient(job, time.Second)

	_, err := uploadOnePart(context.Background(), client, job, job.SourceSize, 1, time.Second, func() error {
		return errDeliverySuperseded
	})
	if !errors.Is(err, errDeliverySuperseded) {
		t.Fatalf("闸门拒绝时必须返回 superseded，实际 %v", err)
	}
	if got := atomic.LoadInt32(&hits); got != 0 {
		t.Fatalf("被顶替作业不得发出任何 PUT，实际 %d 次", got)
	}
}

// TestDiscardDeliveredCopySkipsSuperseded 锁住"不删别人的包"。
// 副本路径按 task_id 命名，删错对象的下场是新作业下一片直接报"本地副本不存在"。
func TestDiscardDeliveredCopySkipsSuperseded(t *testing.T) {
	dir := setupLocalCopyDir(t)
	path := writeCopy(t, dir, "shared.zip", 32, time.Now())
	runner := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-shared", TaskID: "shared", LocalPath: path, Superseded: true}
	runner.jobs[job.JobID] = &deliveryJob{JobID: "dj-shared", TaskID: "shared", LocalPath: path, Phase: phaseUploading}

	runner.discardDeliveredCopy(job)

	mustExist(t, path)
	if job.LocalDeletedAt != "" {
		t.Fatal("未删文件时不得记录 LocalDeletedAt")
	}
}

// TestDiscardLocalCopySkipsSuperseded 是同一约束在拉取失败分支上的复核：
// 残包清理同样会 os.Remove 那个共享路径。
func TestDiscardLocalCopySkipsSuperseded(t *testing.T) {
	dir := setupLocalCopyDir(t)
	path := writeCopy(t, dir, "shared2.zip", 32, time.Now())
	runner := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-shared2", TaskID: "shared2", LocalPath: path, Superseded: true}
	runner.jobs[job.JobID] = &deliveryJob{JobID: "dj-shared2", TaskID: "shared2", LocalPath: path}

	runner.discardLocalCopy(job, "source_still_writing")

	mustExist(t, path)
	if job.IntegrityNote != "" || job.PulledBytes != 0 {
		t.Fatal("资格丧失时连进度状态都不该被重置——那些字节已是新作业的续传基线")
	}
}

// TestRunJobSkipsJobSupersededBeforeCancelRegistration 覆盖"注册取消函数时才发现被顶替"
// 的窄窗口：此时既不能继续推进，也不能把自己注册进 cancels（否则 map 里留下
// 一个永远不会被调用的取消函数）。
func TestRunJobSkipsJobSupersededBeforeCancelRegistration(t *testing.T) {
	r := newTestDeliveryRunner(t)
	job := &deliveryJob{JobID: "dj-race", TaskID: "race", Phase: phaseUploading}
	r.jobs[job.JobID] = &deliveryJob{JobID: "dj-race", TaskID: "race", Phase: phaseWaitingCollect}

	r.runJob(job)

	if job.Phase != phaseUploading {
		t.Fatalf("执行位已被占用的作业不得推进阶段，实际 %s", job.Phase)
	}
	if _, ok := r.cancels[job.JobID]; ok {
		t.Fatal("被拒绝的作业不得把取消函数留在 cancels 里")
	}
}
