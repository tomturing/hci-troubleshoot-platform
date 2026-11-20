package delivery

import "testing"

// 已拉满的作业在 retrying 时必须归成 uploading：归成 pulling 会让一次纯上传失败
// 的重试去要求 SSH 会话，会话被回收后就成了 needs_reauth 终态，而本地副本完好。
func TestNormalizeRetryingWithFullPullGoesUploading(t *testing.T) {
	job := &deliveryJob{
		Phase:       phaseRetrying,
		SourceSize:  2122094416,
		PulledBytes: 2122094416,
	}
	phase, err := normalizeDeliveryPhase(job)
	if err != nil {
		t.Fatalf("retrying 归一化不应报错: %v", err)
	}
	if phase != phaseUploading {
		t.Fatalf("已拉满的作业必须归成 uploading，实际 %s", phase)
	}
	if deliveryPhaseNeedsNode(phase) {
		t.Fatal("uploading 不需要节点，不应因会话缺失被判 needs_reauth")
	}
}

// 未拉满时必须回 pulling：否则会拿半截包去上传。
func TestNormalizeRetryingWithPartialPullGoesPulling(t *testing.T) {
	job := &deliveryJob{Phase: phaseRetrying, SourceSize: 1000, PulledBytes: 400}
	if phase, _ := normalizeDeliveryPhase(job); phase != phasePulling {
		t.Fatalf("未拉满的作业必须归成 pulling，实际 %s", phase)
	}
}
