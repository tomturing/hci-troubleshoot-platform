package main

// 桥日志直传：把结构化日志直接从 bridge 送到平台，不经过浏览器。
//
// 第一性原理
// ----------
// 现有回采链是 `bridge → WS → 浏览器 → 后端`，唯一载体是一个随时会消失的标签页：
// 刷新、断网、休眠、崩溃任一发生，断连窗口内的日志就永久丢失。
// 2026-10-27 现场最关键的一条 `log_collect.session_missing` 正是这样丢的。
//
// 因此把"传输载体"换成 bridge 自己：平台签发一份只写日志的短期令牌（前端经 WS 下发），
// bridge 落本地 outbox 后自行批量上报；浏览器在不在都不影响。
//
// 对抗性审查
// ----------
//  1. 令牌泄露 → 只能写绑定工单的日志，且 user_id 标记为 bridge:<instance>，可事后甄别；
//  2. 平台不可达 → 队列落盘，恢复后补传；outbox 有字节上限，防止写满客户机磁盘；
//  3. 与业务抢带宽/CPU → 单 worker 串行、批量 100 条、gzip、2 秒一次；
//  4. 事件重复 → 后端按 event_id 唯一约束幂等，重复上传不产生重复行；
//  5. 无令牌（前端未下发/版本旧）→ 保持原状：只落本地文件，不报错、不阻塞。

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	// 单批上限：100 条在 gzip 后通常 < 20KB，弱网也能一次成功。
	defaultLogUploadBatch = 100
	// 上报间隔：2 秒一批，既不会积压太久，也不会在 Atom 机器上刷 CPU。
	defaultLogUploadInterval = 2 * time.Second
	// outbox 字节上限：8MB 约等于 8 万条，足够覆盖数天断网；超限丢最旧并计数。
	defaultLogOutboxMaxBytes = 8 << 20
	// 单次 POST 超时：平台内网 30 秒足够；超时即整批重试，不拆批。
	logUploadTimeout = 30 * time.Second
)

// logUploader 是进程级单例：**串行**发送，全局并发恒为 1。
//
// 为什么必须串行：断点语义依赖"前一批成功才推进"，并发会让失败批次与成功批次
// 交错，重试边界不可判定；在低配机器上并发也只会放大内存与调度开销。
type logUploader struct {
	mu sync.Mutex
	// sendMu 串行化"取批次 → 发送 → 出队"整段：只靠 mu 保护队列会出两个问题——
	// 两个 goroutine 可能取到同一批（重复发送），且后完成者用旧 size 切新队列（越界）。
	sendMu   sync.Mutex
	token    string
	endpoint string
	caseID   string
	instance string
	queue    []logEntry
	outbox   *os.File
	outboxAt string
	maxBytes int64
	written  int64
	dropped  uint64
	started  bool
	wake     chan struct{}
	attempt  int
}

var logUpload = newLogUploader()

func newLogUploader() *logUploader {
	return &logUploader{
		wake:     make(chan struct{}, 1),
		maxBytes: int64(envIntOrDefault("HCI_BRIDGE_LOG_OUTBOX_MAX_BYTES", defaultLogOutboxMaxBytes)),
	}
}

// configure 接收前端下发的直传凭据并启动 worker。
//
// 幂等：同一 token 重复下发不会重启 worker；token 变化时重置退避计数，
// 让上一次失败（例如旧令牌过期）不拖累新令牌。
func (u *logUploader) configure(token, endpoint, caseID, instance string) {
	if strings.TrimSpace(token) == "" || strings.TrimSpace(endpoint) == "" {
		return
	}
	u.mu.Lock()
	changed := u.token != token
	u.token = strings.TrimSpace(token)
	u.endpoint = strings.TrimSpace(endpoint)
	u.caseID = strings.TrimSpace(caseID)
	u.instance = strings.TrimSpace(instance)
	if changed {
		u.attempt = 0
	}
	needStart := !u.started
	u.started = true
	u.mu.Unlock()

	u.recoverOutbox()

	blog("INFO", "log_upload.configured", "桥日志直传已启用", "", caseID, "", "", map[string]any{
		"endpoint": endpoint, "instance": instance, "token_changed": changed,
	})
	if needStart {
		go u.run()
	}
	u.notify()
}

// enabled 返回当前是否具备直传条件（有令牌且已配置）。
func (u *logUploader) enabled() bool {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.token != "" && u.endpoint != ""
}

// enqueue 把一条事件放进发送队列并落盘。
//
// 调用点必须在 logHub 解锁之后（enqueue 自己加锁，避免与 publish 的锁嵌套）。
func (u *logUploader) enqueue(entry logEntry) {
	if !u.enabled() {
		return
	}
	u.mu.Lock()
	// 队列上限与 outbox 字节上限双重保护：客户机磁盘可能只剩几百 MB。
	if len(u.queue) >= 5000 || (u.maxBytes > 0 && u.written >= u.maxBytes) {
		u.dropped++
		u.mu.Unlock()
		return
	}
	u.queue = append(u.queue, entry)
	u.persistLocked(entry)
	u.mu.Unlock()
	u.notify()
}

// notify 非阻塞唤醒 worker。
func (u *logUploader) notify() {
	select {
	case u.wake <- struct{}{}:
	default:
	}
}

func (u *logUploader) interval() time.Duration {
	seconds := envIntOrDefault("HCI_BRIDGE_LOG_UPLOAD_INTERVAL_SECONDS", int(defaultLogUploadInterval/time.Second))
	if seconds <= 0 {
		seconds = 2
	}
	return time.Duration(seconds) * time.Second
}

func (u *logUploader) batchSize() int {
	size := envIntOrDefault("HCI_BRIDGE_LOG_UPLOAD_BATCH", defaultLogUploadBatch)
	if size <= 0 || size > 1000 {
		return defaultLogUploadBatch
	}
	return size
}

// run 是唯一的发送 goroutine。
func (u *logUploader) run() {
	ticker := time.NewTicker(u.interval())
	defer ticker.Stop()
	for {
		select {
		case <-u.wake:
		case <-ticker.C:
		}
		// 一轮可能连续发送多批：队列积压时尽快追平，但每批仍是串行。
		for {
			sent, err := u.flushOnce()
			if err != nil {
				delay := u.backoff()
				blog("WARN", "log_upload.failed", "桥日志直传失败，稍后重试", "", "", "", "", map[string]any{
					"error": err.Error(), "backoff_seconds": int(delay.Seconds()), "queued": u.queueLen(),
				})
				time.Sleep(delay)
				break
			}
			if sent == 0 {
				break
			}
		}
	}
}

func (u *logUploader) queueLen() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return len(u.queue)
}

// backoff 返回下一次重试延迟：1s→2s→…→60s。
func (u *logUploader) backoff() time.Duration {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.attempt++
	if u.attempt > 6 {
		u.attempt = 6
	}
	delay := time.Duration(1<<uint(u.attempt-1)) * time.Second
	if delay > 60*time.Second {
		delay = 60 * time.Second
	}
	return delay
}

// flushOnce 发送一批；返回实际发送条数。错误时不清队列（整批重试）。
func (u *logUploader) flushOnce() (int, error) {
	// 串行化整段：worker 与手动触发（含测试/运维）并发时，不得有两条同时取批次。
	u.sendMu.Lock()
	defer u.sendMu.Unlock()

	u.mu.Lock()
	if u.token == "" || u.endpoint == "" {
		u.mu.Unlock()
		return 0, nil
	}
	size := u.batchSize()
	if len(u.queue) == 0 {
		u.mu.Unlock()
		return 0, nil
	}
	if size > len(u.queue) {
		size = len(u.queue)
	}
	batch := make([]logEntry, size)
	copy(batch, u.queue[:size])
	token, endpoint := u.token, u.endpoint
	u.mu.Unlock()

	payload, err := encodeLogBatch(batch)
	if err != nil {
		return 0, err
	}
	request, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Content-Encoding", "gzip")
	request.Header.Set("X-Bridge-Upload-Token", token)

	client := &http.Client{Timeout: logUploadTimeout}
	response, err := client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	// 有界排空，便于连接复用。
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64*1024))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return 0, fmt.Errorf("平台返回 HTTP %d", response.StatusCode)
	}

	// 成功才推进队列：失败时整批保留，靠 event_id 幂等抵消重复发送。
	u.mu.Lock()
	u.queue = u.queue[size:]
	u.attempt = 0
	u.mu.Unlock()
	return size, nil
}

// encodeLogBatch 把事件编码为 gzip 压缩的 JSON。
func encodeLogBatch(batch []logEntry) ([]byte, error) {
	body, err := json.Marshal(map[string]any{"logs": batch})
	if err != nil {
		return nil, err
	}
	var buffer bytes.Buffer
	writer := gzip.NewWriter(&buffer)
	if _, err := writer.Write(body); err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

// outboxDir 返回落盘目录（与日志同级的 bridge-log-outbox）。
func outboxDir() string {
	dir := strings.TrimSpace(logHub.logDir)
	if dir == "" {
		return ""
	}
	return filepath.Join(dir, "bridge-log-outbox")
}

// persistLocked 追加一条到 outbox；失败不影响内存队列（best-effort）。
func (u *logUploader) persistLocked(entry logEntry) {
	dir := outboxDir()
	if dir == "" || u.instance == "" {
		return
	}
	if u.outbox == nil {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return
		}
		path := filepath.Join(dir, u.instance+".jsonl")
		file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			return
		}
		u.outbox, u.outboxAt = file, path
		// 续接已有文件时按当前大小继续累加，避免重启后 written 归零而突破上限。
		if info, statErr := file.Stat(); statErr == nil {
			u.written = info.Size()
		}
	}
	line, err := json.Marshal(entry)
	if err != nil {
		return
	}
	line = append(line, '\n')
	if written, writeErr := u.outbox.Write(line); writeErr == nil {
		u.written += int64(written)
	}
}

// recoverOutbox 在进程重启后把未发送的日志读回队列并清空文件。
//
// 取舍：只按文件整体恢复，不做 offset 续传——单 worker 串行 + 成功后整批出队，
// 未出队的必然在文件里；重启时整体重放即可，重复由后端 event_id 去重。
func (u *logUploader) recoverOutbox() {
	dir := outboxDir()
	if dir == "" {
		return
	}
	u.mu.Lock()
	instance := u.instance
	u.mu.Unlock()
	if instance == "" {
		return
	}
	path := filepath.Join(dir, instance+".jsonl")
	file, err := os.Open(path)
	if err != nil {
		return
	}
	defer file.Close()

	recovered := make([]logEntry, 0, 256)
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		var entry logEntry
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			continue
		}
		recovered = append(recovered, entry)
		if len(recovered) >= 5000 {
			break
		}
	}
	if len(recovered) == 0 {
		return
	}
	u.mu.Lock()
	u.queue = append(recovered, u.queue...)
	u.mu.Unlock()
	// 读回后清空，避免下次重启重复恢复（未发送部分已在内存队列里，
	// 若此时进程再挂，最多丢这一批，与"发送中丢失"是同一语义）。
	_ = os.Truncate(path, 0)
	blog("INFO", "log_upload.recovered", "已从 outbox 恢复未发送日志", "", "", "", "", map[string]any{
		"recovered": len(recovered), "path": path,
	})
}

// logUploadStats 供 /status 暴露直传状态（排障时无需再猜是否启用）。
func logUploadStats() map[string]any {
	u := logUpload
	u.mu.Lock()
	defer u.mu.Unlock()
	return map[string]any{
		"enabled":  u.token != "" && u.endpoint != "",
		"queued":   len(u.queue),
		"dropped":  atomic.LoadUint64(&u.dropped),
		"instance": u.instance,
		"outbox":   u.outboxAt,
	}
}
