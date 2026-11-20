package main

// 端到端完整性校验的进程内守卫。
//
// 这些用例的共同对手是"看起来校验了、其实没比"——例如：
//   - 摘要命令在节点上拼错，输出为空却仍被当成"通过"；
//   - 解析时把 32 位 md5 当 sha256 用，长度自洽性被绕过；
//   - 拉取大小相等但内容被同名覆盖，只做 size 判断就继续上传。
// 因此每个用例都断言"错的那一侧必须报错"，而不是只断言成功路径。

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestVerifyPulledIntegrity 每一条错误都必须被识别为 fatal。
func TestVerifyPulledIntegrity(t *testing.T) {
	sha := strings.Repeat("a", 64)
	md5 := strings.Repeat("b", 32)
	other := strings.Repeat("c", 64)

	if err := verifyPulledIntegrity(sha, "", sha, "", 100, 100); err != nil {
		t.Fatalf("摘要一致不应报错: %v", err)
	}
	// 大小写不同应视为一致（节点输出可能是大写十六进制）。
	if err := verifyPulledIntegrity(strings.ToUpper(sha), "", sha, "", 100, 100); err != nil {
		t.Fatalf("大小写差异不应报错: %v", err)
	}
	if err := verifyPulledIntegrity(sha, "", other, "", 100, 100); err == nil || !isDeliveryFatalError(err) {
		t.Fatalf("SHA-256 不一致必须 fatal，实际 %v", err)
	}
	if err := verifyPulledIntegrity("", md5, "", md5, 100, 100); err != nil {
		t.Fatalf("MD5 一致不应报错: %v", err)
	}
	if err := verifyPulledIntegrity("", md5, "", strings.Repeat("d", 32), 100, 100); err == nil || !isDeliveryFatalError(err) {
		t.Fatalf("MD5 不一致必须 fatal，实际 %v", err)
	}
	if err := verifyPulledIntegrity(sha, "", sha, "", 99, 100); err == nil || !isDeliveryFatalError(err) {
		t.Fatalf("大小不一致必须 fatal，实际 %v", err)
	}
	if err := verifyPulledIntegrity("", "", sha, "", 100, 100); err != nil {
		t.Fatalf("无基线时不得报错（只能降级为 unverified）: %v", err)
	}
}

// TestBuildCompletePayloadDeclaresDigest complete 必须带摘要，但绝不能用空串表达"没有"。
func TestBuildCompletePayloadDeclaresDigest(t *testing.T) {
	source := strings.Repeat("a", 64)
	local := strings.Repeat("b", 64)

	payload := buildCompletePayload(&deliveryJob{SourceSize: 100, SourceSHA256: source, LocalSHA256: local})
	if payload["sha256"] != source {
		t.Fatalf("应优先声明节点基线，实际 %v", payload["sha256"])
	}
	if payload["size"] != int64(100) {
		t.Fatalf("size 缺失: %v", payload["size"])
	}

	payload = buildCompletePayload(&deliveryJob{SourceSize: 100, LocalSHA256: local})
	if payload["sha256"] != local {
		t.Fatalf("无基线时应退本地摘要，实际 %v", payload["sha256"])
	}

	payload = buildCompletePayload(&deliveryJob{})
	if _, exists := payload["sha256"]; exists {
		t.Fatalf("没有摘要时字段必须缺省（空串会触发 422）: %v", payload["sha256"])
	}
	if _, exists := payload["size"]; exists {
		t.Fatalf("size=0 时字段必须缺省: %v", payload["size"])
	}
}

// TestDiscardLocalCopyResetsProgress 残包必须删除，分片进度必须清零。
//
// 只删文件不清进度同样危险：平台侧还留着按旧包算的分片，续传会跳过它们，
// 最终拼出"前半旧包 + 后半新包"的杂交产物。
func TestDiscardLocalCopyResetsProgress(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "archive.zip")
	if err := os.WriteFile(path, []byte("partial"), 0o600); err != nil {
		t.Fatal(err)
	}
	runner := &deliveryRunner{store: newDeliveryStore("")}
	job := &deliveryJob{JobID: "dj-test", LocalPath: path, PulledBytes: 7, UploadedParts: []int{1, 2, 3}}

	runner.discardLocalCopy(job, "摘要不一致")

	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("残包未删除: err=%v", err)
	}
	if job.PulledBytes != 0 || len(job.UploadedParts) != 0 {
		t.Fatalf("进度未重置: pulled=%d parts=%v", job.PulledBytes, job.UploadedParts)
	}
	if job.Integrity != integrityUnverified || job.IntegrityNote == "" {
		t.Fatalf("必须降级并留痕: integrity=%q note=%q", job.Integrity, job.IntegrityNote)
	}
}
