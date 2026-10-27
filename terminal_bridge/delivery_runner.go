package main

// 交付作业执行器：进程内唯一的串行调度者。
//
// 为什么必须"唯一"：交付是 20GB 级的长任务，两路并发会在客户机上互相拖到双双超时
// （2026-10-27 现场内存 95%、CPU 83%）。跨任务并发默认 1，只有高配机才允许 2；
// 任务内的分片并发由资源探测决定，见 delivery_concurrency.go。
//
// 与浏览器彻底解耦：submit 之后所有阶段都在这里推进，页面关闭不影响；
// 前端只通过 job_status 轮询进度，重连后继续轮询即可。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// 等待 acli 采集完成的上限：单次采集超 4 小时基本可以判死，避免 job 永久占位。
	deliveryCollectTimeout = 4 * time.Hour
	// 单个 job 的拉取+上传整体预算：20GiB @ 100Mbps 约 27 分钟，给足重试余量。
	deliveryJobTimeout = 6 * time.Hour
	// 每传多少片落一次盘：太频繁会把磁盘打满（5120 片 × 写盘），太稀疏会丢进度。
	deliverySaveEveryParts = 10
)

// deliveryRunner 是进程级单例。
type deliveryRunner struct {
	bridge      *Bridge
	store       *deliveryStore
	concurrency deliveryConcurrency

	mu       sync.Mutex
	jobs     map[string]*deliveryJob
	cancels  map[string]context.CancelFunc
	queue    chan *deliveryJob
	started  bool
	inflight int
}

var deliveryRunnerInstance *deliveryRunner

// newDeliveryRunner 构造执行器；store 目录来自日志目录（与 job 文件同级，便于现场打包取证）。
func newDeliveryRunner(bridge *Bridge) *deliveryRunner {
	runner := &deliveryRunner{
		bridge:      bridge,
		store:       newDeliveryStore(logHub.logDir),
		concurrency: decideDeliveryConcurrency(),
		jobs:        make(map[string]*deliveryJob),
		cancels:     make(map[string]context.CancelFunc),
		queue:       make(chan *deliveryJob, 16),
	}
	return runner
}

// start 启动调度器并恢复未完成的 job。
//
// 恢复策略：平台任务状态优先（已完成直接归档），其余按落盘阶段继续；
// 读不出来的 job 文件跳过并留痕，绝不阻塞其它 job。
func (r *deliveryRunner) start() {
	r.mu.Lock()
	if r.started {
		r.mu.Unlock()
		return
	}
	r.started = true
	r.mu.Unlock()

	blog("INFO", "delivery.runner_started", "交付执行器已启动", "", "", "", "",
		describeDeliveryConcurrency(r.concurrency))

	go r.schedule()

	recovered := r.store.list()
	for _, job := range recovered {
		r.mu.Lock()
		r.jobs[job.JobID] = job
		r.mu.Unlock()
		blog("INFO", "delivery.recovered", "恢复未完成的交付作业", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "task_id": job.TaskID, "phase": string(job.Phase),
			"pulled_bytes": job.PulledBytes, "uploaded_parts": len(job.UploadedParts),
			"needs_reauth": job.NeedsReauth,
		})
		// needs_reauth 的 job 不自动入队：没有凭据跑起来只会立刻失败，等前端重连后再唤醒。
		if !job.NeedsReauth {
			r.enqueue(job)
		}
	}
	// 终态 job 文件保留 7 天供取证，之后清理。
	if removed := r.store.purgeOlderThan(7 * 24 * time.Hour); removed > 0 {
		blog("INFO", "delivery.purged", "清理过期交付作业记录", "", "", "", "", map[string]any{"removed": removed})
	}
}

// enqueue 非阻塞入队；队列满时保持 job 在 store 里等下一轮启动，不丢。
func (r *deliveryRunner) enqueue(job *deliveryJob) {
	select {
	case r.queue <- job:
	default:
		blog("WARN", "delivery.queue_full", "交付队列已满，作业留待下一轮", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "phase": string(job.Phase),
		})
	}
}

// schedule 按「跨任务并发」限制串行消费队列。
func (r *deliveryRunner) schedule() {
	semaphore := make(chan struct{}, r.concurrency.Tasks)
	for job := range r.queue {
		semaphore <- struct{}{}
		go func(job *deliveryJob) {
			defer func() { <-semaphore }()
			r.runJob(job)
		}(job)
	}
}

// submit 接收前端下发的交付请求：落盘 + 入队，立即返回 job_id（不再让前端等 2 小时）。
func (r *deliveryRunner) submit(job *deliveryJob) *deliveryJob {
	if job.JobID == "" {
		job.JobID = "dj-" + job.TaskID
	}
	job.CreatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	job.Phase = phaseWaitingCollect
	r.mu.Lock()
	// 同 task 重复提交（前端刷新后重放）直接复用既有 job，避免重复占位。
	if existing, ok := r.jobs[job.JobID]; ok && !existing.Phase.terminal() {
		r.mu.Unlock()
		return existing
	}
	r.jobs[job.JobID] = job
	r.mu.Unlock()

	if err := r.store.save(job); err != nil {
		blog("WARN", "delivery.save_failed", "交付作业落盘失败（本次仅内存）", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "error": err.Error(),
		})
	}
	blog("INFO", "delivery.submitted", "交付作业已提交", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "task_id": job.TaskID, "source_path": job.SourcePath,
		"source_size": job.SourceSize, "part_count": job.PartCount, "chunk_size": job.ChunkSize,
	})
	r.enqueue(job)
	return job
}

// status 返回 job 快照（深拷贝，避免调用方读到并发修改中的结构）。
func (r *deliveryRunner) status(jobID string) *deliveryJob {
	r.mu.Lock()
	job, ok := r.jobs[jobID]
	r.mu.Unlock()
	if !ok {
		// 内存里没有就回查磁盘：进程重启后前端仍会按 job_id 轮询。
		job = r.store.load(jobID)
		if job == nil {
			return nil
		}
	}
	snapshot := *job
	snapshot.UploadedParts = append([]int(nil), job.UploadedParts...)
	return &snapshot
}

// resume 在用户重连后唤醒 needs_reauth 的 job。
func (r *deliveryRunner) resume(jobID string) bool {
	r.mu.Lock()
	job, ok := r.jobs[jobID]
	if !ok {
		r.mu.Unlock()
		return false
	}
	job.NeedsReauth = false
	job.LastError = ""
	r.mu.Unlock()
	_ = r.store.save(job)
	r.enqueue(job)
	return true
}

// cancel 取消正在跑的 job（用户显式取消，或平台任务已被取消）。
func (r *deliveryRunner) cancel(jobID string) bool {
	r.mu.Lock()
	cancel, running := r.cancels[jobID]
	job := r.jobs[jobID]
	r.mu.Unlock()
	if running {
		cancel()
	}
	if job == nil {
		return false
	}
	job.Phase = phaseFailed
	job.LastError = "cancelled_by_user"
	_ = r.store.save(job)
	return true
}

// runJob 推进单个 job 到终态。
func (r *deliveryRunner) runJob(job *deliveryJob) {
	ctx, cancel := context.WithTimeout(context.Background(), deliveryJobTimeout)
	r.mu.Lock()
	r.cancels[job.JobID] = cancel
	r.mu.Unlock()
	defer func() {
		cancel()
		r.mu.Lock()
		delete(r.cancels, job.JobID)
		r.mu.Unlock()
	}()

	err := r.advance(ctx, job)
	r.mu.Lock()
	if err != nil {
		job.Attempt++
		job.LastError = err.Error()
		// 凭据类失败单独标记：它不是"重试能好"的错误，必须等人。
		if isDeliveryAuthError(err) {
			job.NeedsReauth = true
		} else if job.Attempt < 5 {
			// 非凭据失败自动重排一次，靠退避等待链路恢复。
			job.Phase = phaseFailed
		} else {
			job.Phase = phaseFailed
		}
	} else {
		job.Attempt = 0
		job.LastError = ""
	}
	finalPhase := job.Phase
	finalAttempt := job.Attempt
	needsReauth := job.NeedsReauth
	r.mu.Unlock()

	_ = r.store.save(job)
	blog("INFO", "delivery.job_finished", "交付作业状态更新", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "task_id": job.TaskID, "phase": string(finalPhase),
		"attempt": finalAttempt, "needs_reauth": needsReauth, "error": job.LastError,
		"pulled_bytes": job.PulledBytes, "uploaded_parts": len(job.UploadedParts),
	})

	// 可恢复的失败按退避重排，避免弱网下一抖就判死。
	if err != nil && !needsReauth && finalAttempt < 5 {
		delay := time.Duration(1<<uint(finalAttempt)) * time.Second
		if delay > 60*time.Second {
			delay = 60 * time.Second
		}
		go func() {
			time.Sleep(delay)
			// 重排前把阶段恢复成"可继续"的形态：拉取/上传各自续传，不回到 waiting_collect。
			r.mu.Lock()
			if job.Phase == phaseFailed && !job.NeedsReauth {
				if len(job.UploadedParts) > 0 {
					job.Phase = phaseUploading
				} else if job.PulledBytes > 0 {
					job.Phase = phasePulling
				}
			}
			r.mu.Unlock()
			r.enqueue(job)
		}()
	}
}

// advance 按阶段推进；每个阶段都可重入。
func (r *deliveryRunner) advance(ctx context.Context, job *deliveryJob) error {
	session := r.sessionFor(job)
	if session == nil {
		return &deliveryAuthError{reason: "SSH 会话不存在（需重新连接节点）"}
	}

	switch job.Phase {
	case phaseWaitingCollect, "":
		if err := r.waitCollect(ctx, session, job); err != nil {
			return err
		}
		fallthrough
	case phasePulling:
		if err := r.pull(ctx, session, job); err != nil {
			return err
		}
		fallthrough
	case phaseUploading:
		if err := r.upload(ctx, job); err != nil {
			return err
		}
		fallthrough
	case phaseCompleting:
		if err := r.complete(ctx, job); err != nil {
			return err
		}
	}

	r.mu.Lock()
	job.Phase = phaseDone
	r.mu.Unlock()
	return nil
}

// sessionFor 按 case+node 解析会话；失败返回 nil（由调用方转成 needs_reauth）。
func (r *deliveryRunner) sessionFor(job *deliveryJob) *SSHSession {
	session, _, _ := r.bridge.resolveLogCollectSession(InMessage{CaseID: job.CaseID, NodeIP: job.NodeIP})
	return session
}

// waitCollect 只读 progress，直到 acli 报 done == total。
//
// 硬约束：**绝不 cancel、绝不发 create**——采集是节点侧的事，交付只负责搬运。
func (r *deliveryRunner) waitCollect(ctx context.Context, session *SSHSession, job *deliveryJob) error {
	deadline := time.Now().Add(deliveryCollectTimeout)
	interval := 10 * time.Second
	for {
		if time.Now().After(deadline) {
			return fmt.Errorf("等待采集完成超时（%s）", deliveryCollectTimeout)
		}
		output, _, err := session.runCommandPty(ctx, "acli log_collect progress", 60*time.Second)
		if err == nil {
			progress := parseLogCollectProgress(output)
			if progress.ParseOK && progress.Total > 0 && progress.Done >= progress.Total {
				r.mu.Lock()
				if job.SourcePath == "" {
					job.SourcePath = progress.ArchivePath
				}
				job.Phase = phasePulling
				r.mu.Unlock()
				_ = r.store.save(job)
				blog("INFO", "delivery.collect_finished", "节点采集已完成，开始交付", "", job.CaseID, job.NodeIP, "", map[string]any{
					"job_id": job.JobID, "total": progress.Total, "archive_path": progress.ArchivePath,
				})
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
		// 自适应降频：采集通常十几分钟，10s→30s 减少对节点与桥的打扰。
		if interval < 30*time.Second {
			interval += 10 * time.Second
		}
	}
}

// pull 把包从 HCI 拉到本地；断点依据是本地文件大小（tail -c +N）。
func (r *deliveryRunner) pull(ctx context.Context, session *SSHSession, job *deliveryJob) error {
	if strings.TrimSpace(job.SourcePath) == "" {
		return fmt.Errorf("缺少源包路径，无法拉取")
	}
	if job.LocalPath == "" {
		job.LocalPath = filepath.Join(os.TempDir(), "hci-log-collect", job.TaskID+".zip")
	}
	// 恢复场景：size 未知时先量一次，否则无法判断"是否已拉完"。
	if job.SourceSize <= 0 {
		output, _, err := session.runQuickCommand(ctx, fmt.Sprintf("stat -c %%s %s", shellQuote(job.SourcePath)))
		if err == nil {
			if size, parseErr := strconv.ParseInt(strings.TrimSpace(output), 10, 64); parseErr == nil {
				job.SourceSize = size
			}
		}
	}
	written, err := session.pullArchive(ctx, job.SourcePath, job.LocalPath, job.SourceSize, func(done, total int64) {
		r.mu.Lock()
		job.PulledBytes = done
		if job.SourceSize <= 0 && total > 0 {
			job.SourceSize = total
		}
		r.mu.Unlock()
		_ = r.store.save(job)
	})
	if err != nil {
		return fmt.Errorf("拉取中断（已拉 %d 字节）: %w", written, err)
	}
	r.mu.Lock()
	job.PulledBytes = written
	job.Phase = phaseUploading
	r.mu.Unlock()
	_ = r.store.save(job)
	return nil
}

// upload 分片上传；并发度由资源决定，已传分片自动跳过。
func (r *deliveryRunner) upload(ctx context.Context, job *deliveryJob) error {
	if job.UploadToken == "" || job.PlatformURL == "" {
		return &deliveryAuthError{reason: "缺少上传令牌或平台地址"}
	}
	if job.PartCount <= 0 || job.ChunkSize <= 0 {
		return fmt.Errorf("分片参数缺失（part_count=%d chunk_size=%d）", job.PartCount, job.ChunkSize)
	}
	info, err := os.Stat(job.LocalPath)
	if err != nil {
		return fmt.Errorf("本地副本不存在: %w", err)
	}
	totalSize := info.Size()

	uploaded := job.uploadedSet()
	parts := chunkConcurrencyFor(r.concurrency, job.ChunkSize, availableMemoryMB())
	client := &http.Client{Timeout: logCollectPartTimeout}

	var (
		errMu     sync.Mutex
		firstErr  error
		pending   = make(chan int)
		wg        sync.WaitGroup
		sinceSave int
	)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	worker := func() {
		defer wg.Done()
		for number := range pending {
			if ctx.Err() != nil {
				return
			}
			if err := uploadOnePart(ctx, client, job, totalSize, number); err != nil {
				errMu.Lock()
				if firstErr == nil {
					firstErr = err
				}
				errMu.Unlock()
				cancel()
				return
			}
			// job 是共享结构（status() 会并发读快照），所有修改必须在 r.mu 下完成。
			r.mu.Lock()
			job.addUploadedPart(number)
			sinceSave++
			shouldSave := sinceSave >= deliverySaveEveryParts
			if shouldSave {
				sinceSave = 0
			}
			r.mu.Unlock()
			if shouldSave {
				_ = r.store.save(job)
			}
		}
	}

	wg.Add(parts)
	for i := 0; i < parts; i++ {
		go worker()
	}
	for number := 1; number <= job.PartCount; number++ {
		if uploaded[number] {
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

	r.mu.Lock()
	job.Phase = phaseCompleting
	r.mu.Unlock()
	_ = r.store.save(job)

	if firstErr != nil {
		return fmt.Errorf("上传中断（已传 %d/%d 片）: %w", len(job.UploadedParts), job.PartCount, firstErr)
	}
	return nil
}

// uploadOnePart 上传单片（含 SHA-256 与重试）。
func uploadOnePart(ctx context.Context, client *http.Client, job *deliveryJob, totalSize int64, number int) error {
	offset := int64(number-1) * job.ChunkSize
	expected := job.ChunkSize
	if offset+expected > totalSize {
		expected = totalSize - offset
	}
	if expected <= 0 {
		return fmt.Errorf("分片 %d 大小异常", number)
	}
	file, err := os.Open(job.LocalPath)
	if err != nil {
		return err
	}
	defer file.Close()
	payload := make([]byte, expected)
	if _, readErr := file.ReadAt(payload, offset); readErr != nil && readErr != io.EOF {
		return readErr
	}
	digest := sha256Hex(payload)

	target := fmt.Sprintf("%s%s/%s/parts/%d", normalizePlatformURL(job.PlatformURL), logCollectDirectUploadPrefix, job.TaskID, number)
	var lastErr error
	for attempt := 1; attempt <= logCollectPartRetries; attempt++ {
		lastErr = putPartBytes(ctx, client, target, job.UploadToken, digest, payload)
		if lastErr == nil {
			return nil
		}
		if attempt == logCollectPartRetries {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(attempt) * time.Second):
		}
	}
	return fmt.Errorf("分片 %d 上传失败: %w", number, lastErr)
}

// complete 调平台数据面收尾（用上传令牌，不需要客户 Cookie），平台随后自动做代传。
func (r *deliveryRunner) complete(ctx context.Context, job *deliveryJob) error {
	body, err := json.Marshal(map[string]any{
		"parts":  job.UploadedParts,
		"size":   job.SourceSize,
		"sha256": "",
	})
	if err != nil {
		return err
	}
	target := fmt.Sprintf("%s%s/%s/complete", normalizePlatformURL(job.PlatformURL), logCollectDirectUploadPrefix, job.TaskID)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, target, strings.NewReader(string(body)))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Upload-Token", job.UploadToken)

	client := &http.Client{Timeout: logCollectPartTimeout}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64*1024))
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return &deliveryAuthError{reason: fmt.Sprintf("上传令牌失效（HTTP %d）", response.StatusCode)}
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("平台收尾失败 HTTP %d", response.StatusCode)
	}
	return nil
}

// deliveryAuthError 表示"重试无用，必须人工/前端介入"的失败。
type deliveryAuthError struct{ reason string }

func (e *deliveryAuthError) Error() string { return e.reason }

func isDeliveryAuthError(err error) bool {
	_, ok := err.(*deliveryAuthError)
	return ok
}

// sha256Hex 计算分片校验和（与服务端 X-Part-SHA256 对齐）。
func sha256Hex(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// putPartBytes 上传单个分片。
func putPartBytes(ctx context.Context, client *http.Client, target, token, digest string, payload []byte) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, target, strings.NewReader(string(payload)))
	if err != nil {
		return err
	}
	request.Header.Set("X-Upload-Token", token)
	request.Header.Set("X-Part-SHA256", digest)
	request.Header.Set("Content-Type", "application/octet-stream")
	request.ContentLength = int64(len(payload))
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64*1024))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("平台返回 HTTP %d", response.StatusCode)
	}
	return nil
}
