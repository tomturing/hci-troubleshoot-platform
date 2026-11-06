package main

import (
	"context"
	"fmt"
	"net"
	"testing"
	"time"
)

// 现场基线：8 MiB 分片、1.94 GB 包、总上行约 34 KB/s、并发 2 路。
// 这是 Q2026110600112 的真实数字，所有用例都以它为"必须能收敛"的锚点。
const (
	fieldChunkSize = int64(8 * 1024 * 1024)
	fieldRateBps   = int64(34 * 1024)
	fieldCeiling   = 2
)

func TestNextUploadPaceNoTimeoutKeepsPace(t *testing.T) {
	prev := defaultUploadPace(2, logCollectPartTimeout)
	next := nextUploadPace(prev, uploadRoundSample{
		UploadedParts: 3,
		UploadedBytes: 3 * fieldChunkSize,
		Elapsed:       60 * time.Second,
	}, fieldChunkSize, fieldCeiling)
	if next != prev {
		t.Fatalf("没有超时证据时不应改节奏，得到 %+v", next)
	}
}

func TestNextUploadPaceNoProgressHalvesPartsAndDoublesTimeout(t *testing.T) {
	prev := defaultUploadPace(2, logCollectPartTimeout)
	next := nextUploadPace(prev, uploadRoundSample{TimedOutParts: 1, Elapsed: 15 * time.Minute}, fieldChunkSize, fieldCeiling)
	if next.Parts != 1 {
		t.Fatalf("一片没过时应把并发降到 1，得到 %d", next.Parts)
	}
	if next.PartTimeout != int64((2*logCollectPartTimeout)/time.Second) {
		t.Fatalf("超时应翻倍，得到 %d 秒", next.PartTimeout)
	}
	if next.Reason != "no_progress_escalation" {
		t.Fatalf("reason 应标记为无进展升级，得到 %q", next.Reason)
	}
}

func TestNextUploadPaceNoProgressStopsAtFloorAndCeiling(t *testing.T) {
	pace := defaultUploadPace(1, logCollectPartTimeout)
	for i := 0; i < 10; i++ {
		pace = nextUploadPace(pace, uploadRoundSample{TimedOutParts: 1}, fieldChunkSize, fieldCeiling)
	}
	if pace.Parts != 1 {
		t.Fatalf("并发不得低于 1，得到 %d", pace.Parts)
	}
	if pace.PartTimeout != int64(paceMaxPartTimeout/time.Second) {
		t.Fatalf("超时应封顶在 %s，得到 %d 秒", paceMaxPartTimeout, pace.PartTimeout)
	}
}

// 现场核心用例：总吞吐 34 KB/s、并发 2、8 MiB 片、300 s 预算——本轮有片成功也有片超时。
// 修复前：并发 2 → 单路 17 KB/s → 单片需 493 s > 300 s，每轮必然空转。
// 修复后：必须收敛到单路，并把预算抬到 493×1.6≈789 s 以上，使单片能过。
func TestNextUploadPaceFieldWeakLinkConverges(t *testing.T) {
	prev := defaultUploadPace(2, logCollectPartTimeout)
	sample := uploadRoundSample{
		UploadedParts: 1,
		UploadedBytes: fieldChunkSize,
		TimedOutParts: 1,
		// 2 片 × 8 MiB 在 34 KB/s 上共约 482 s（1 片成功 + 1 片超时）。
		Elapsed: 482 * time.Second,
	}
	next := nextUploadPace(prev, sample, fieldChunkSize, fieldCeiling)
	if next.Parts != 1 {
		t.Fatalf("弱网应收敛到单路，得到 %d 路", next.Parts)
	}
	needSingle := float64(fieldChunkSize) / float64(fieldRateBps) // 约 241 s
	if float64(next.PartTimeout) < needSingle {
		t.Fatalf("预算必须覆盖单路实际耗时 %.0fs，得到 %ds", needSingle, next.PartTimeout)
	}
	if next.PartTimeout > int64(paceMaxPartTimeout/time.Second) {
		t.Fatalf("预算不得超过封顶，得到 %d", next.PartTimeout)
	}
	if next.Reason == "" || next.Reason == "default" {
		t.Fatalf("reason 应带实测速率，得到 %q", next.Reason)
	}
}

// 链路更差（总 17 KB/s）：单片需要约 493 s，预算应继续抬到能覆盖它。
func TestNextUploadPaceExtremelySlowLinkRaisesTimeout(t *testing.T) {
	prev := uploadPace{Parts: 1, PartTimeout: int64(logCollectPartTimeout / time.Second)}
	sample := uploadRoundSample{
		UploadedParts: 1,
		UploadedBytes: fieldChunkSize,
		TimedOutParts: 2,
		// 总 17 KB/s：2 片 × 8 MiB 约 964 s。
		Elapsed: 964 * time.Second,
	}
	next := nextUploadPace(prev, sample, fieldChunkSize, fieldCeiling)
	if next.PartTimeout <= int64(logCollectPartTimeout/time.Second) {
		t.Fatalf("极慢链路必须抬高预算，得到 %d 秒", next.PartTimeout)
	}
}

// 只升不降：预算已被证明过小（本轮确实有片超时），任何情况下都不能改小。
func TestNextUploadPaceNeverLowersTimeout(t *testing.T) {
	prev := uploadPace{Parts: 1, PartTimeout: 900}
	sample := uploadRoundSample{
		UploadedParts: 20,
		UploadedBytes: 20 * fieldChunkSize,
		TimedOutParts: 1,
		Elapsed:       20 * time.Second, // 看起来极快
	}
	next := nextUploadPace(prev, sample, fieldChunkSize, fieldCeiling)
	if next.PartTimeout < prev.PartTimeout {
		t.Fatalf("有超时证据时不得下调预算，%d -> %d", prev.PartTimeout, next.PartTimeout)
	}
}

func TestUploadPaceResolveDefendsCorruptValues(t *testing.T) {
	// 旧版本作业文件里没有 pace 字段：零值必须落回默认节奏，而不是 0 路并发死等。
	got := uploadPace{}.resolve(3)
	if got.Parts != 1 || got.PartTimeout != int64(logCollectPartTimeout/time.Second) || got.Reason != "default" {
		t.Fatalf("零值归一化错误: %+v", got)
	}
	// 手改坏的超上限/超下限值。
	got = uploadPace{Parts: 99, PartTimeout: 999999}.resolve(2)
	if got.Parts != 2 || got.PartTimeout != int64(paceMaxPartTimeout/time.Second) {
		t.Fatalf("越界值归一化错误: %+v", got)
	}
	got = uploadPace{Parts: -3, PartTimeout: 1}.resolve(4)
	if got.Parts != 1 || got.PartTimeout != int64(logCollectPartTimeout/time.Second) {
		t.Fatalf("负值归一化错误: %+v", got)
	}
}

func TestNextUploadPaceIgnoresUnknownChunkSize(t *testing.T) {
	prev := defaultUploadPace(2, logCollectPartTimeout)
	if got := nextUploadPace(prev, uploadRoundSample{TimedOutParts: 3}, 0, fieldCeiling); got != prev {
		t.Fatalf("分片大小未知时不应调整，得到 %+v", got)
	}
}

func TestIsPartTimeoutError(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"ctx 超时", context.DeadlineExceeded, true},
		// 现场原文，来自 Go 的 http.Client.Timeout。
		{"client 超时", fmt.Errorf(`Put "https://x/parts/20": context deadline exceeded (Client.Timeout exceeded while awaiting headers)`), true},
		{"i/o 超时", fmt.Errorf("dial tcp 10.0.0.1:443: i/o timeout"), true},
		{"网络错误", fmt.Errorf("connection reset by peer"), false},
		{"平台 5xx", fmt.Errorf("平台返回 HTTP 503"), false},
		{"凭据失败", &deliveryAuthError{reason: "令牌过期"}, false},
	}
	for _, c := range cases {
		if got := isPartTimeoutError(c.err); got != c.want {
			t.Fatalf("%s: 期望 %v，得到 %v", c.name, c.want, got)
		}
	}
	// net.Error.Timeout 路径也必须是超时。
	if !isPartTimeoutError(timeoutNetError{}) {
		t.Fatal("net.Error.Timeout 应判定为超时")
	}
}

type timeoutNetError struct{}

func (timeoutNetError) Error() string   { return "fake timeout" }
func (timeoutNetError) Timeout() bool   { return true }
func (timeoutNetError) Temporary() bool { return true }

var _ net.Error = timeoutNetError{}
