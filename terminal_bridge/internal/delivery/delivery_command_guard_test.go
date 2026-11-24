package delivery

import (
	"testing"
	"time"
)

// 连续失败才判死：偶发一次命令失败（节点正忙）不应把作业推去重排。
func TestCommandFailTrackerRequiresConsecutiveFailures(t *testing.T) {
	tracker := newCommandFailTracker(3)
	if streak, dead := tracker.fail(); dead || streak != 1 {
		t.Fatalf("第 1 次失败：streak=%d dead=%v，期望 streak=1 dead=false", streak, dead)
	}
	if streak, dead := tracker.fail(); dead || streak != 2 {
		t.Fatalf("第 2 次失败：streak=%d dead=%v，期望 streak=2 dead=false", streak, dead)
	}
	if streak, dead := tracker.fail(); !dead || streak != 3 {
		t.Fatalf("第 3 次失败：streak=%d dead=%v，期望 streak=3 dead=true", streak, dead)
	}
}

// 一次成功就把计数清零：否则长时间的"失败-成功-失败"抖动会被累加成假死。
func TestCommandFailTrackerResetsOnSuccess(t *testing.T) {
	tracker := newCommandFailTracker(3)
	tracker.fail()
	tracker.fail()
	tracker.reset()
	if tracker.streakNow() != 0 {
		t.Fatalf("reset 后 streak=%d，期望 0", tracker.streakNow())
	}
	if _, dead := tracker.fail(); dead {
		t.Fatal("reset 后的第一次失败不应判死")
	}
}

// 阈值配错不能变成"永远不判死"：<=0 退回默认阈值。
func TestCommandFailTrackerFallsBackToDefaultLimit(t *testing.T) {
	tracker := newCommandFailTracker(0)
	for i := 0; i < deliveryNodeCommandFailureLimit-1; i++ {
		if _, dead := tracker.fail(); dead {
			t.Fatalf("第 %d 次失败就判死，期望阈值 %d", i+1, deliveryNodeCommandFailureLimit)
		}
	}
	if _, dead := tracker.fail(); !dead {
		t.Fatalf("达到默认阈值 %d 次仍未判死", deliveryNodeCommandFailureLimit)
	}
}

// 等待/重试阶段必须报 waiting：报 fetching 会让面板显示"上传中 0/5 片"，
// 把"还没开工"说成"传不动"，现场会去查带宽与磁盘。
func TestTransferStageForWaitingPhases(t *testing.T) {
	cases := []struct {
		name     string
		phase    deliveryPhase
		uploaded int64
		want     string
	}{
		{name: "等待采集", phase: phaseWaitingCollect, want: "waiting"},
		{name: "空阶段按等待处理", phase: "", want: "waiting"},
		{name: "失败后等重排", phase: phaseRetrying, want: "waiting"},
		{name: "拉取中", phase: phasePulling, want: "fetching"},
		{name: "上传中", phase: phaseUploading, want: "uploading"},
		{name: "有已传分片即使阶段未更新也算上传", phase: phasePulling, uploaded: 8388608, want: "uploading"},
	}
	for _, tc := range cases {
		if got := transferStageFor(tc.phase, tc.uploaded); got != tc.want {
			t.Errorf("%s：stage=%q，期望 %q", tc.name, got, tc.want)
		}
	}
}

// 探测类命令的预算必须远小于作业总时长，否则"永久挂起"会被上层误认为"还在干活"。
func TestDeliveryProbeTimeoutIsShort(t *testing.T) {
	if deliveryNodeProbeTimeout <= 0 || deliveryNodeProbeTimeout > time.Minute {
		t.Fatalf("deliveryNodeProbeTimeout=%s，期望 (0, 1m]，探测命令本身是毫秒级操作", deliveryNodeProbeTimeout)
	}
	if deliveryNodeProbeTimeout >= deliveryCollectTimeout {
		t.Fatalf("探测预算 %s 不应接近等待采集总时长 %s", deliveryNodeProbeTimeout, deliveryCollectTimeout)
	}
}
