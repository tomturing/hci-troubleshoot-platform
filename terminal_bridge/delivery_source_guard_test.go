package main

import (
	"strings"
	"testing"
)

// TestSourceSizeGuard 钉住「源包在交付期间被重新生成」的判定与错误性质。
//
// 现场回归：Q2026103000994 提交时 69,010,358 字节 / 9 片，交付过程中包被重新
// 生成成 172,261,027 字节；upload 按新大小切片，平台按旧会话校验，末片从
// 1,901,494 字节变成 8,388,608 字节，每片 500、重试 8 轮必然失败。
func TestSourceSizeGuard(t *testing.T) {
	cases := []struct {
		name      string
		submitted int64
		current   int64
		wantErr   bool
	}{
		{name: "一致时放行", submitted: 69010358, current: 69010358},
		{name: "老前端未上报基线时不做判断", submitted: 0, current: 172261027},
		{name: "拿不到当前大小时不做判断", submitted: 69010358, current: 0},
		{name: "包被重新生成（变大）必须拦下", submitted: 69010358, current: 172261027, wantErr: true},
		{name: "包被截断（变小）必须拦下", submitted: 172261027, current: 69010358, wantErr: true},
		{name: "只差 1 字节也必须拦下", submitted: 69010358, current: 69010359, wantErr: true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := sourceSizeGuard(tc.submitted, tc.current)
			if !tc.wantErr {
				if err != nil {
					t.Fatalf("不应报错，实际: %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("应拦下但放行了（submitted=%d current=%d）", tc.submitted, tc.current)
			}
			// 必须是 fatal：重试只会把同一个错误重复 N 轮，还白拉一次几十 GB。
			if !isDeliveryFatalError(err) {
				t.Fatalf("必须是 deliveryFatalError（不重试），实际类型: %T", err)
			}
			// 也不能被当成 transient 进入退避重排。
			if isDeliveryTransientError(err) {
				t.Fatalf("不得被识别为 transient: %v", err)
			}
			msg := err.Error()
			for _, want := range []string{"源包已变化", "请重新点击", "本次上传已作废"} {
				if !strings.Contains(msg, want) {
					t.Fatalf("错误文案缺少 %q，实际: %s", want, msg)
				}
			}
			// 这条最容易回归：文案把责任推给平台，用户就会去查平台日志，而真正
			// 的原因是包变了。文案里必须出现两个具体字节数，便于现场判断是否重采。
			if !strings.Contains(msg, "69010358") && !strings.Contains(msg, "172261027") {
				t.Fatalf("错误文案应包含具体字节数，实际: %s", msg)
			}
			if strings.Contains(msg, "平台暂时不可用") {
				t.Fatalf("不得把源包变化说成平台不可用: %s", msg)
			}
		})
	}
}
