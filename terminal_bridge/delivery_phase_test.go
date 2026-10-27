package main

// 交付阶段机的回归护栏。
//
// 这里锁的是两条历史缺陷，它们都不表现为"报错"，而是表现为**状态说谎**：
//  1. phase 为 failed 时 advance 的 switch 无匹配，控制流直接掉到
//     `job.Phase = phaseDone`，把"还没拉到第一个字节就失败"的作业标记成交付成功；
//  2. resume 只清 needs_reauth 不清阶段，于是用户点"继续"之后立刻以
//     "终态拒绝推进"再次失败，等于没点。
// 状态类缺陷无法靠人工点一遍发现，只能靠断言把语义钉住。

import (
	"strings"
	"testing"
)

func TestNormalizeDeliveryPhase(t *testing.T) {
	cases := []struct {
		name    string
		job     deliveryJob
		want    deliveryPhase
		wantErr string
	}{
		{name: "空阶段按等待采集处理", job: deliveryJob{}, want: phaseWaitingCollect},
		{name: "等待采集保持", job: deliveryJob{Phase: phaseWaitingCollect}, want: phaseWaitingCollect},
		{name: "拉取保持", job: deliveryJob{Phase: phasePulling}, want: phasePulling},
		{name: "上传保持", job: deliveryJob{Phase: phaseUploading}, want: phaseUploading},
		{name: "合并保持", job: deliveryJob{Phase: phaseCompleting}, want: phaseCompleting},
		{
			name: "重试且有已传分片则从上传续",
			job:  deliveryJob{Phase: phaseRetrying, UploadedParts: []int{1, 2}},
			want: phaseUploading,
		},
		{
			name: "重试且已拉过字节则从拉取续",
			job:  deliveryJob{Phase: phaseRetrying, PulledBytes: 4096},
			want: phasePulling,
		},
		{
			name: "重试且毫无进度则从头开始",
			job:  deliveryJob{Phase: phaseRetrying},
			want: phaseWaitingCollect,
		},
		{
			// 关键回归：终态绝不能被"推进"，否则会被改写成 done。
			name:    "已完成拒绝推进",
			job:     deliveryJob{Phase: phaseDone},
			want:    phaseDone,
			wantErr: "终态",
		},
		{
			// 关键回归：失败态过去会掉进 switch 外、直达 done。
			name:    "已失败拒绝推进",
			job:     deliveryJob{Phase: phaseFailed},
			want:    phaseFailed,
			wantErr: "终态",
		},
		{
			name:    "未知阶段拒绝推进",
			job:     deliveryJob{Phase: deliveryPhase("weird")},
			want:    deliveryPhase("weird"),
			wantErr: "未知",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizeDeliveryPhase(&tc.job)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("不应报错，实际: %v", err)
				}
			} else {
				if err == nil {
					t.Fatalf("应报错（含 %q），实际 nil，返回阶段 %s", tc.wantErr, got)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("错误应包含 %q，实际: %v", tc.wantErr, err)
				}
			}
			if got != tc.want {
				t.Fatalf("阶段应为 %s，实际 %s", tc.want, got)
			}
		})
	}
}

func TestResumeMovesJobOutOfTerminalPhase(t *testing.T) {
	runner := &deliveryRunner{
		store: newDeliveryStore(t.TempDir()),
		jobs:  map[string]*deliveryJob{},
		queue: make(chan *deliveryJob, 4),
	}
	job := &deliveryJob{
		JobID:       "dj-resume",
		TaskID:      "task-1",
		Phase:       phaseFailed,
		NeedsReauth: true,
		LastError:   "上传令牌失效（HTTP 401）",
	}
	runner.jobs[job.JobID] = job

	if !runner.resume(job.JobID) {
		t.Fatal("resume 应返回 true")
	}
	if job.NeedsReauth {
		t.Fatal("resume 后不应仍需重新认证")
	}
	if job.Phase != phaseRetrying {
		t.Fatalf("resume 后阶段应为 retrying，实际 %s（停留在终态会让 advance 立刻拒绝推进）", job.Phase)
	}

	select {
	case got := <-runner.queue:
		if got != job {
			t.Fatalf("入队的应是同一个作业，实际 %v", got)
		}
	default:
		t.Fatal("resume 应把作业重新入队")
	}
}

func TestProgressKeepsValueWhileRetrying(t *testing.T) {
	// 重试中把进度打回 0% 会让用户以为"全丢了"，而分片在平台、字节在本地。
	job := &deliveryJob{
		Phase:         phaseRetrying,
		PartCount:     10,
		UploadedParts: []int{1, 2, 3, 4, 5},
		SourceSize:    1000,
		PulledBytes:   1000,
	}
	if got := job.progress(); got != 75 {
		t.Fatalf("重试中进度应为 75（已传一半），实际 %d", got)
	}
}
