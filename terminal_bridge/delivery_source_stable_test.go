package main

import (
	"strings"
	"testing"
)

// 同一任务失败后重试时，平台会签发新令牌/新 total_size/新分片方案。
// 若只按 task_id 复用旧作业，SubmittedSize 会停在上一轮，新会话每片都校验不过，
// 且重试永远不会自愈（2026-11-04 Q2026110400883 的复盘结论）。
func TestSameDeliverySessionDetectsNewPlatformSession(t *testing.T) {
	base := &deliveryJob{
		JobID: "dj-t1", TaskID: "t1", UploadToken: "tok-a",
		SubmittedSize: 882701883, PartCount: 106, ChunkSize: 8 << 20, Direct: true,
	}
	replay := *base // 前端刷新/重复点击后的同会话重放
	if !sameDeliverySession(base, &replay) {
		t.Fatal("同一会话的重放必须复用，否则会重复占位")
	}

	cases := map[string]func(j *deliveryJob){
		"新上传令牌":   func(j *deliveryJob) { j.UploadToken = "tok-b" },
		"新会话基线":   func(j *deliveryJob) { j.SubmittedSize = 892074444 },
		"新分片数":    func(j *deliveryJob) { j.PartCount = 107 },
		"新分片大小":   func(j *deliveryJob) { j.ChunkSize = 16 << 20 },
		"直传与中转切换": func(j *deliveryJob) { j.Direct = false },
	}
	for name, mutate := range cases {
		next := *base
		mutate(&next)
		if sameDeliverySession(base, &next) {
			t.Fatalf("%s 后不得复用旧作业：新会话按旧基线校验必然逐片失败", name)
		}
	}
}

// 源包仍在写入必须带规范码，前端/平台才能按码分支；
// 只靠中文文案分支会在文案改动时静默失效（008 的教训）。
func TestFatalErrorCarriesDeliveryCode(t *testing.T) {
	err := newFatal(ErrTransferSourceStillWriting, "源包仍在写入（%d → %d 字节）", 882701883, 892074444)
	if !isDeliveryFatalError(err) {
		t.Fatal("必须仍被识别为 fatal：重试只会追一个移动的目标")
	}
	if got := deliveryErrorCode(err); got != ErrTransferSourceStillWriting {
		t.Fatalf("必须映射成 %s，实际 %q", ErrTransferSourceStillWriting, got)
	}
	message := err.Error()
	for _, fragment := range []string{"882701883", "892074444"} {
		if !strings.Contains(message, fragment) {
			t.Fatalf("文案必须带具体字节数便于现场判断是否重采: %s", message)
		}
	}
	// 没有码的 fatal 不能被误映射成 010。
	if got := deliveryErrorCode(&deliveryFatalError{reason: "无码失败"}); got != "" {
		t.Fatalf("无码 fatal 不应产生错误码，实际 %q", got)
	}
}
