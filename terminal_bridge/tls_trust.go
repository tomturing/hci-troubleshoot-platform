package main

// 平台证书信任判定与 HTTP 客户端构造（交付通道与桥日志直传通道共用）。
//
// 第一性原理：客户内网平台普遍使用**自签证书**，客户机的系统根证书库不可能预置
// 这份 CA。任何"用系统信任链去校验平台"的代码路径，在这类环境里都是必然失败——
// 不是网络问题，不是配置问题，是前提不成立。
//
// 因此两条通道各自明确选择策略：
//   - 交付通道（分片上传）：由用户显式授权后放宽，授权随 job 落盘；
//   - 桥日志直传通道：只写日志、只带只写令牌，遇自签自动放宽一次并留痕，
//     否则"平台不可观测"会成为常态（2026-10-27 现场连续 x509，排查时只能靠人肉拷贝终端）。
//
// 共同点：都只放宽 TLS 校验，不放松鉴权——令牌仍绑定 task_id / 工单。

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net/http"
	"time"
)

// isUntrustedCertificateError 判断是否为"证书不受信"类失败。
//
// 必须能穿透 url.Error 包装：Go 的 http.Client 返回的握手错误是
// `*url.Error`，直接比对类型会发现不了里面的 x509 原因。
func isUntrustedCertificateError(err error) bool {
	if err == nil {
		return false
	}
	var certErr *tls.CertificateVerificationError
	if errors.As(err, &certErr) {
		return true
	}
	var authorityErr x509.UnknownAuthorityError
	if errors.As(err, &authorityErr) {
		return true
	}
	var hostnameErr x509.HostnameError
	if errors.As(err, &hostnameErr) {
		return true
	}
	return false
}

// httpClientFor 构造 HTTP 客户端；insecure 为真时跳过服务端证书校验。
//
// 刻意做成"按调用点决定"而不是全局开关：放宽的范围必须与授权范围等长，
// 否则一次放宽会永久污染整个进程。
func httpClientFor(insecure bool, timeout time.Duration) *http.Client {
	client := &http.Client{Timeout: timeout}
	if !insecure {
		return client
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{
		// #nosec G402 -- 客户内网自签平台，放宽范围限定在单次调用/单个 endpoint
		InsecureSkipVerify: true,
	}
	client.Transport = transport
	return client
}
