package main

// 交付阶段与会话依赖的回归护栏。
//
// 现场 Q2026102800720（3.6 GiB，2026-10-28）：平台收尾连续 4 次 500 的几分钟里
// SSH 会话被回收，第 5 次重试在"上传/收尾"阶段被当成凭据失效直接判死。
// 但上传只用本地副本、收尾只访问平台 HTTP，这两个阶段根本不需要节点。
// 一刀切地要求会话，等于让"节点连接掉了"去否决一件与节点无关的事。

import "testing"

func TestDeliveryPhaseNeedsNode(t *testing.T) {
	cases := map[deliveryPhase]bool{
		phaseWaitingCollect: true,
		phasePulling:        true,
		phaseUploading:      false,
		phaseCompleting:     false,
	}
	for phase, want := range cases {
		if got := deliveryPhaseNeedsNode(phase); got != want {
			t.Fatalf("phase=%s 是否需要节点：期望 %v，实际 %v", phase, want, got)
		}
	}
}

func TestRequireSessionForPhase(t *testing.T) {
	// 不需要节点的阶段：会话缺失只能算普通失败（可重试），不能标 needs_reauth。
	for _, phase := range []deliveryPhase{phaseUploading, phaseCompleting} {
		if err := requireSessionForPhase(phase, nil); err != nil {
			t.Fatalf("phase=%s 不应因缺少 SSH 会话判死，实际 %v", phase, err)
		}
	}

	// 需要节点的阶段：没有会话必须落到"需要重新认证"，否则前端不会引导重连。
	for _, phase := range []deliveryPhase{phaseWaitingCollect, phasePulling} {
		err := requireSessionForPhase(phase, nil)
		if err == nil {
			t.Fatalf("phase=%s 缺少会话必须报错", phase)
		}
		if !isDeliveryAuthError(err) {
			t.Fatalf("phase=%s 的错误必须是 needs_reauth 语义，实际 %T", phase, err)
		}
	}

	// 有会话时任何阶段都不该被拦。
	for _, phase := range []deliveryPhase{phaseWaitingCollect, phasePulling, phaseUploading, phaseCompleting} {
		if err := requireSessionForPhase(phase, &SSHSession{}); err != nil {
			t.Fatalf("phase=%s 有会话时不应报错：%v", phase, err)
		}
	}
}
