package uplink

// 云面分片上传原语（桥→平台 HTTP，不依赖 Runner；架构重构设计 §3.4 修订 1）。
//
// 合并 delivery upload/uploadOnePart 与 logcollect uploadArchiveParts 两份实现：
// 字节逻辑（读片、摘要、PUT、重试退避、已传跳过）唯一化；编排差异用 PushSpec
// 显式表达——并发数、单片共享预算、资格闸门、落盘回调、错误分类各由调用方注入，
// 原语不认识任何业务域的job/错误类型。

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	// PartRetries 单片重试次数（含首次）。
	PartRetries = 3
	// DefaultPartTimeout 单片默认超时：20 GiB @100 Mbps ≈ 27 分钟整包，
	// 单片 8 MiB 留 5 分钟覆盖弱网与重试。
	DefaultPartTimeout = 5 * time.Minute
	// DirectUploadPrefix 平台数据面默认路径，与 case-service 路由保持一致。
	DirectUploadPrefix = "/api/direct/log-collect-uploads"
)

// NormalizeBaseURL 去掉尾部斜杠，避免拼出 //api/... 被网关判成异常路径。
func NormalizeBaseURL(raw string) string {
	return strings.TrimRight(strings.TrimSpace(raw), "/")
}

// PutPart 上传单个分片的裸 HTTP PUT：返回状态码与传输层错误，
// 错误分类（401/403 鉴权、transient 包装等）是业务域语义，由 PushSpec.Classify 注入。
func PutPart(ctx context.Context, client *http.Client, target, token, sha256Hex string, payload []byte) (int, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, target, bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	request.Header.Set("X-Upload-Token", token)
	request.Header.Set("X-Part-SHA256", sha256Hex)
	request.Header.Set("Content-Type", "application/octet-stream")
	request.ContentLength = int64(len(payload))

	response, err := client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)
	return response.StatusCode, nil
}

// PushSpec 描述一次分片上传会话的全部输入。
type PushSpec struct {
	// BaseURL 已归一化的平台基址；Prefix 数据面前缀；TaskID 平台会话标识。
	BaseURL string
	Prefix  string
	TaskID  string
	Token   string

	LocalPath string
	ChunkSize int64
	PartCount int
	// TotalSize 本地副本实测大小；>0 时末片按它截断。
	TotalSize int64
	// Uploaded 历史已传分片（续传跳过）。
	Uploaded map[int]bool

	// Workers 并发 worker 数；<=1 顺序上传。
	Workers int
	// PartBudget 单片总墙钟预算（所有重试共享）；>0 启用共享预算模式，
	// ==0 时每次尝试各自受 Client.Timeout 约束（顺序直传通道口径）。
	PartBudget time.Duration
	Client     *http.Client

	// Fence 资格闸门：读盘分配缓冲前与每次真正 PUT 前各查一次；
	// 被顶替的作业迟到的 PUT 会改写新会话的同名分片，平台 epoch fencing
	// 是第一道防线，这里是不依赖对端版本的本地防线。nil 表示不设闸。
	Fence func() error
	// OnPart 单片成功回调（worker 内并发调用，调用方自行加锁落盘）。
	OnPart func(number int, written int64)
	// Classify 把 (状态码, 传输错误) 映射成业务错误；nil 用缺省分类
	// （传输错误原样、非 2xx 报 "平台返回 HTTP %d"）。
	Classify func(status int, transportErr error) error
}

// PushResult 一轮上传的可观测事实集（供 pace 收敛）与首错。
type PushResult struct {
	DoneParts     int
	DoneBytes     int64
	TimedOutParts int
	Elapsed       time.Duration
	// Err 首个分片错误；外部取消且无分片错误时为 nil（调用方结合 ctx 判定）。
	Err error
}

// Push 分片上传；并发与单片预算按实测速率收敛，已传分片自动跳过。
func Push(ctx context.Context, spec PushSpec) PushResult {
	workers := spec.Workers
	if workers < 1 {
		workers = 1
	}
	classify := spec.Classify
	if classify == nil {
		classify = func(status int, transportErr error) error {
			if transportErr != nil {
				return transportErr
			}
			if status < 200 || status >= 300 {
				return fmt.Errorf("平台返回 HTTP %d", status)
			}
			return nil
		}
	}

	var (
		errMu    sync.Mutex
		firstErr error
		pending  = make(chan int)
		wg       sync.WaitGroup

		// 本轮实测样本：只在这里被累加，用于推导下一轮节奏。
		statMu         sync.Mutex
		roundDoneParts int
		roundDoneBytes int64
		roundTimedOut  int
	)
	roundStart := time.Now()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	worker := func() {
		defer wg.Done()
		for number := range pending {
			if ctx.Err() != nil {
				return
			}
			written, err := pushOnePart(ctx, spec, classify, number)
			if err != nil {
				// 先取作业自身状态再 cancel：只有"作业还活着时超时"才算链路证据。
				// 被 watchdog 或绝对上限掐断的作业若被记成弱网证据，
				// 会把一个已经判死的作业的节奏也一起拖到最保守档，下一轮白等 20 分钟。
				jobAlive := ctx.Err() == nil
				errMu.Lock()
				if firstErr == nil {
					firstErr = err
				}
				errMu.Unlock()
				if jobAlive && IsPartTimeoutError(err) {
					statMu.Lock()
					roundTimedOut++
					statMu.Unlock()
				}
				cancel()
				return
			}
			statMu.Lock()
			roundDoneParts++
			roundDoneBytes += written
			statMu.Unlock()
			if spec.OnPart != nil {
				spec.OnPart(number, written)
			}
		}
	}

	wg.Add(workers)
	for i := 0; i < workers; i++ {
		go worker()
	}
	for number := 1; number <= spec.PartCount; number++ {
		if spec.Uploaded[number] {
			continue
		}
		select {
		case <-ctx.Done():
			break
		case pending <- number:
		}
		if ctx.Err() != nil {
			break
		}
	}
	close(pending)
	wg.Wait()

	return PushResult{
		DoneParts:     roundDoneParts,
		DoneBytes:     roundDoneBytes,
		TimedOutParts: roundTimedOut,
		Elapsed:       time.Since(roundStart),
		Err:           firstErr,
	}
}

// pushOnePart 上传单片（含 SHA-256 与重试），返回实际提交的字节数。
//
// PartBudget 是"这一片的总墙钟预算"，由 pacing 按实测速率给出，**所有重试共享它**。
// 改造前是"每次尝试各给 300 s"，弱网下单片最坏要烧 15 分钟才肯放手：
// 一轮 3 次重试刚好吃满 15 分钟空转，作业就在这个循环里永远推进不了一格。
// 共享预算后，单片最坏占用 = budget，重试只在"早早失败"时才有意义（那才是它该救的场景）。
func pushOnePart(ctx context.Context, spec PushSpec, classify func(int, error) error, number int) (int64, error) {
	// 分片要整片读进内存（8 MiB），所以在分配之前就先问一次资格：
	// 已被顶替的作业连读盘都不该做，更不该把内存占住让新作业起不来 worker。
	if spec.Fence != nil {
		if err := spec.Fence(); err != nil {
			return 0, err
		}
	}
	offset := int64(number-1) * spec.ChunkSize
	expected := spec.ChunkSize
	if spec.TotalSize > 0 && offset+expected > spec.TotalSize {
		expected = spec.TotalSize - offset
	}
	if expected <= 0 {
		return 0, fmt.Errorf("分片 %d 大小异常", number)
	}
	file, err := os.Open(spec.LocalPath)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	payload := make([]byte, expected)
	if _, readErr := file.ReadAt(payload, offset); readErr != nil && readErr != io.EOF {
		return 0, fmt.Errorf("读取分片 %d 失败: %w", number, readErr)
	}
	digest := sha256.Sum256(payload)

	attemptCtx := ctx
	if spec.PartBudget > 0 {
		var cancel context.CancelFunc
		attemptCtx, cancel = context.WithTimeout(ctx, spec.PartBudget)
		defer cancel()
	}

	target := fmt.Sprintf("%s%s/%s/parts/%d", spec.BaseURL, spec.Prefix, spec.TaskID, number)
	var lastErr error
	for attempt := 1; attempt <= PartRetries; attempt++ {
		// 每次真正发 PUT 之前重新确认资格（理由见 PushSpec.Fence）。
		if spec.Fence != nil {
			if err := spec.Fence(); err != nil {
				return 0, err
			}
		}
		status, transportErr := PutPart(attemptCtx, spec.Client, target, spec.Token, hex.EncodeToString(digest[:]), payload)
		lastErr = classify(status, transportErr)
		if lastErr == nil {
			return expected, nil
		}
		if attempt == PartRetries {
			break
		}
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-attemptCtx.Done():
			// 预算耗尽：再用同一条已证明不够的链路重放，只会把这一轮剩余时间也吃掉。
			// 共享预算模式下 attemptCtx 独立于 ctx；顺序模式两者相同，
			// 走到这里即外部取消，与旧顺序实现返回 ctx.Err() 口径一致。
			if spec.PartBudget > 0 {
				return 0, fmt.Errorf("分片 %d 上传失败: %w", number, lastErr)
			}
			return 0, ctx.Err()
		case <-time.After(time.Duration(attempt) * time.Second):
		}
	}
	return 0, fmt.Errorf("分片 %d 上传失败: %w", number, lastErr)
}
