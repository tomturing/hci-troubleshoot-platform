package main

// 交付失败分类与退避窗口的护栏。
//
// 现场回归（Q2026102701193，2026-10-27 18:29Z）：平台正在滚动更新，
// bridge 的 5 次 complete 全部撞在空窗内（18:29:20–18:29:46），而新 Pod
// 18:29:21 才创建、18:29:41 才启动完成 —— 5 次预算（累计约 31 秒）比空窗还短，
// 于是一次"平台发版"把用户的上传判成了终态失败。
//
// 这里锁两条约定：
//  1. "平台暂时不可用"必须与"请求本身不合法"分开，前者值得等；
//  2. 退避窗口必须长于一次滚动更新（分钟级），不能是几十秒。

import (
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"testing"
	"time"
)

func TestDeliveryBackoffCoversDeployWindow(t *testing.T) {
	// 2+4+8+16+32+60+60 = 182s，必须显著长于一次 K8s 滚动更新（30~90s）
	var total time.Duration
	for attempt := 1; attempt < maxDeliveryTransientAttempts; attempt++ {
		total += deliveryBackoff(attempt)
	}
	if total < 120*time.Second {
		t.Fatalf("暂时不可用场景的累计退避只有 %s，覆盖不了一次滚动更新空窗", total)
	}
	if got := deliveryBackoff(1); got != 2*time.Second {
		t.Fatalf("首次退避应为 2s，实际 %s", got)
	}
	if got := deliveryBackoff(99); got != 60*time.Second {
		t.Fatalf("退避上限应为 60s，实际 %s", got)
	}
	if deliveryBackoff(0) != deliveryBackoff(1) {
		t.Fatal("非法 attempt 应回落到首次退避，而不是负值或零")
	}
}

func TestPlatformStatusErrorClassification(t *testing.T) {
	cases := []struct {
		status    int
		transient bool
		auth      bool
	}{
		{http.StatusServiceUnavailable, true, false},
		{http.StatusBadGateway, true, false},
		{http.StatusGatewayTimeout, true, false},
		{http.StatusTooManyRequests, true, false},
		{http.StatusUnprocessableEntity, false, false},
		{http.StatusBadRequest, false, false},
	}

	for _, tc := range cases {
		err := platformStatusError("平台收尾失败", tc.status)
		if got := isDeliveryTransientError(err); got != tc.transient {
			t.Errorf("HTTP %d 的 transient 判定应为 %v，实际 %v（%v）", tc.status, tc.transient, got, err)
		}
		if got := isDeliveryAuthError(err); got != tc.auth {
			t.Errorf("HTTP %d 的 auth 判定应为 %v，实际 %v", tc.status, tc.auth, got)
		}
		// 文案必须保留原始语义，现场靠它定位
		if tc.status == http.StatusServiceUnavailable && err.Error() == "" {
			t.Error("错误文案不能为空")
		}
	}
}

func TestErrorClassificationSurvivesWrapping(t *testing.T) {
	// upload() 会用 %w 包装底层错误；类型断言在此失效，必须用 errors.As。
	authErr := fmt.Errorf("上传中断（已传 3/15 片）: %w", &deliveryAuthError{reason: "上传令牌失效（HTTP 401）"})
	if !isDeliveryAuthError(authErr) {
		t.Fatal("被包装的 auth 错误必须仍能识别，否则令牌失效会退化成盲重试")
	}

	transientErr := fmt.Errorf("上传中断（已传 3/15 片）: %w",
		&deliveryTransientError{reason: "平台收尾失败 HTTP 503（平台暂时不可用，将退避重试）"})
	if !isDeliveryTransientError(transientErr) {
		t.Fatal("被包装的 transient 错误必须仍能识别")
	}
	if isDeliveryAuthError(transientErr) {
		t.Fatal("transient 不应被误判成需要人工介入")
	}
}

func TestTransportErrorClassification(t *testing.T) {
	// 自签证书：url.Error 同时实现 net.Error，若不先摘出证书错误，
	// x509 会被当成"可重试"，用户白等三分钟才看到同样的失败。
	certWrapped := &url.Error{
		Op:  "Post",
		URL: "https://acli.sangfor.com.cn/api/direct/log-collect-uploads/x/complete",
		Err: x509.UnknownAuthorityError{},
	}
	if isTransientTransportError(certWrapped) {
		t.Fatal("证书不受信不是暂时性故障，不应重试")
	}
	if isTransientTransportError(x509.UnknownAuthorityError{}) {
		t.Fatal("裸 x509 错误同样不应重试")
	}

	// 连接超时 / DNS 超时属于暂时性故障
	timeout := &net.DNSError{Err: "i/o timeout", Name: "acli.sangfor.com.cn", IsTimeout: true}
	if !isTransientTransportError(timeout) {
		t.Fatal("DNS 超时应判为暂时性故障")
	}
	if isTransientTransportError(errors.New("分片 3 大小异常")) {
		t.Fatal("业务性错误不应被当成网络暂时故障")
	}
}
