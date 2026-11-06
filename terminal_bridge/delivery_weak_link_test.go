package main

// 弱网收敛的端到端验收。
//
// 为什么必须真跑一遍：Q2026110600112 的死结不是"某一处判断写错"，而是一个
// **不等式在并发下必然不成立**——总上行 34 KB/s 被 2 路均分，8 MiB 一片需要
// 约 440 s，而单片预算只有 300 s，于是每一轮都精确地在同一片上空转。
// 任何只做纯函数断言的单测都证明不了"下一轮真的能过去"。
//
// 这里用一个**共享带宽池**模拟那根被切细的管子：所有并发请求排队领取配额，
// 并发越高单路越慢——这正是现场死结的物理成因，而不是简单的固定延迟。

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// bandwidthLimiter 是全局共享的上行配额：谁先到谁先拿，总量固定。
type bandwidthLimiter struct {
	mu   sync.Mutex
	rate int64 // 字节/秒
	next time.Time
}

func newBandwidthLimiter(bytesPerSecond int64) *bandwidthLimiter {
	return &bandwidthLimiter{rate: bytesPerSecond, next: time.Now()}
}

// wait 为 n 字节申请配额，必要时阻塞到配额可用。
// 返回非 nil 表示等待期间客户端已断开（ctx 取消），调用方直接放弃本次响应。
func (l *bandwidthLimiter) wait(ctx context.Context, n int) error {
	l.mu.Lock()
	now := time.Now()
	if l.next.Before(now) {
		l.next = now
	}
	l.next = l.next.Add(time.Duration(int64(n) * int64(time.Second) / l.rate))
	d := time.Until(l.next)
	l.mu.Unlock()

	if d <= 0 {
		return nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// idle 把配额复位，模拟"新一轮开始时链路是空的"。
func (l *bandwidthLimiter) idle() {
	l.mu.Lock()
	l.next = time.Now()
	l.mu.Unlock()
}

// drainThrottled 按 8 KiB 粒度读请求体，每读一块就向共享池申请配额。
func drainThrottled(ctx context.Context, body io.Reader, limiter *bandwidthLimiter) (int64, error) {
	buf := make([]byte, 8<<10)
	var total int64
	for {
		n, err := body.Read(buf)
		if n > 0 {
			if werr := limiter.wait(ctx, n); werr != nil {
				return total, werr
			}
			total += int64(n)
		}
		if err != nil {
			if err == io.EOF {
				return total, nil
			}
			return total, err
		}
	}
}

// TestWeakLinkUploadConvergesToSingleStreamAndSucceeds 是现场死结的回归用例。
//
// 现场参数：8 MiB/片、300 s/片、2 路并发、总上行 34 KB/s → 单路 19 KB/s，
// 每片需 440 s > 300 s，永远过不去。
// 用例把三个量等比缩小：64 KiB/片、1 s/片、2 路、总上行 40 KB/s（单路 20 KB/s，
// 每片需 3.2 s > 1 s）。比例关系与现场一致，跑一轮只要 1 s。
//
// 验收标准（两条都必须成立）：
//  1. 第一轮必须**一片都传不上去**，否则用例没有复现死结，后面全是假通过；
//  2. 第二轮必须把两片都传完，且节奏已被收敛成单路——这是"自适应真的解开了死结"
//     的唯一证据。只断言"发生了重试"没有意义：原地重试正是当时的故障。
func TestWeakLinkUploadConvergesToSingleStreamAndSucceeds(t *testing.T) {
	// 生产下限是 5 分钟，会让用例跑十几分钟；临时改成 1 秒，
	// 生产路径读到的仍是 logCollectPartTimeout。
	prevMin := paceMinPartTimeout
	paceMinPartTimeout = time.Second
	defer func() { paceMinPartTimeout = prevMin }()

	const chunkSize = 64 << 10
	const partCount = 2
	const totalSize = chunkSize * partCount
	// 60 KB/s 总带宽：2 路各 30 KB/s（每片 2.1 s，1 s 预算必超时）；
	// 收到单路后 60 KB/s（每片 1.07 s，2 s 预算可以通过，留出 HTTP 开销余量）。
	limiter := newBandwidthLimiter(60 << 10)

	var (
		receivedMu sync.Mutex
		received   []int64
		putHits    int32
		// inflight 统计仍在服务端处理的请求：轮次之间必须等它们真正退出，
		// 否则第一轮被掐断的请求还攥着带宽配额，第二轮的"单路"其实是排在被它们后面。
		inflight sync.WaitGroup
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		inflight.Add(1)
		defer inflight.Done()
		atomic.AddInt32(&putHits, 1)
		n, err := drainThrottled(r.Context(), r.Body, limiter)
		if err != nil {
			// 客户端超时先断开：这片没有送达，不算平台收到。
			return
		}
		receivedMu.Lock()
		received = append(received, n)
		receivedMu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	job := prepareJob(t, srv.URL, "task-weak", "tok-weak", totalSize, chunkSize)
	// 现场节奏：2 路、1 秒预算（等比缩小后的 300 s）。
	job.Pace = uploadPace{Parts: 2, PartTimeout: 1, Reason: "field:34KB/s*2"}
	runner := newTestDeliveryRunner(t)

	firstErr := runner.upload(context.Background(), job)
	if firstErr == nil {
		t.Fatal("第一轮在 1 秒预算 × 2 路下不应成功，否则用例没有复现现场死结")
	}
	t.Logf("第一轮错误: %v（是否超时: %v）", firstErr, isPartTimeoutError(firstErr))
	if got := len(job.UploadedParts); got != 0 {
		t.Fatalf("第一轮应一片都传不上去（现场 19/233 之后再无推进），实际成功 %d 片", got)
	}
	// 冻结现场快照，避免第二轮的成功把它盖掉。
	firstRoundHits := atomic.LoadInt32(&putHits)

	next := job.Pace
	if next.Parts != 1 {
		t.Fatalf("无进展的一轮必须把并发降到单路，实际 %d 路", next.Parts)
	}
	if next.PartTimeout != 2 {
		t.Fatalf("无进展的一轮必须把单片预算翻倍到 2 秒，实际 %d", next.PartTimeout)
	}
	t.Logf("第一轮：%d 次 PUT 全部超时，节奏收敛为 %d 路/%d 秒（%s）",
		firstRoundHits, next.Parts, next.PartTimeout, next.Reason)

	// 第二轮：链路空闲后单路应能把两片都送完。
	waitInflightIdle(t, &inflight)
	limiter.idle()
	if err := runner.upload(context.Background(), job); err != nil {
		t.Fatalf("收敛到单路后第二轮必须成功，实际 %v", err)
	}
	receivedMu.Lock()
	gotParts := len(received)
	receivedMu.Unlock()
	if gotParts != partCount {
		t.Fatalf("平台应收到 %d 片，实际 %d 片", partCount, gotParts)
	}
	if got := len(job.UploadedParts); got != partCount {
		t.Fatalf("作业应记录 %d 片已传，实际 %d 片", partCount, got)
	}
	t.Logf("第二轮：%d 路/%d 秒送达 %d 片，弱网死结解开",
		job.Pace.Parts, job.Pace.PartTimeout, gotParts)
}

// waitInflightIdle 等所有在途请求退出，超时即判失败——
// 卡住不退出说明服务端没在客户端断开时及时放手，那是链路模型的缺陷，不是被测代码的。
func waitInflightIdle(t *testing.T, wg *sync.WaitGroup) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("第一轮的请求在客户端断开后仍未退出，带宽配额会被它们继续占着")
	}
}

// TestWeakLinkNoProgressKeepsEscalatingUntilSingleStream 补一条"多轮升级"的边界：
// 并发 4 路在极窄链路上要连续降两次才到单路，中间任何一步都不得把预算改小。
func TestWeakLinkNoProgressKeepsEscalatingUntilSingleStream(t *testing.T) {
	prevMin := paceMinPartTimeout
	paceMinPartTimeout = time.Second
	defer func() { paceMinPartTimeout = prevMin }()

	pace := uploadPace{Parts: 4, PartTimeout: 1, Reason: "field"}
	sample := uploadRoundSample{TimedOutParts: 2, Elapsed: 5 * time.Second}
	for i := 0; i < 4; i++ {
		pace = nextUploadPace(pace, sample, 8<<20, 8)
	}
	if pace.Parts != 1 {
		t.Fatalf("连续无进展必须收敛到单路，实际 %d 路", pace.Parts)
	}
	if pace.timeout() < 8*time.Second {
		t.Fatalf("每一轮都必须放大预算，实际 %s", pace.timeout())
	}
}
