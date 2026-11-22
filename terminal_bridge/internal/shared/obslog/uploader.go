package obslog

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
//  2. 平台不可达 → 队列落盘，恢复后补传；outbox 分段有总磁盘预算，防写满客户机磁盘；
//  3. 与业务抢带宽/CPU → 单 worker 串行、批量 100 条、gzip、2 秒一次；
//  4. 事件重复 → 后端按 event_id 唯一约束幂等，重复上传不产生重复行；
//  5. 无令牌（前端未下发/版本旧）→ 保持原状：只落本地文件，不报错、不阻塞。
//
// L1-L4 完备性增强（设计 §3.6）：目标 = 本地保留窗口内条目至少一次必达平台。
// 内存分两条队列：seedQueue（补传/stderr 包装，仅内存，源头在本地日志文件）与
// queue（实时 enqueue，与分段 outbox 的磁盘游标一一对应）。flush 先消费 seedQueue；
// seq 水位只在实时队列批次成功后推进（补传批次的低 seq 不得把水位拉过未发送的高 seq）；
// stderr 字节水位按批次内 StderrOffset 推进。溢出不再静默丢弃：内存满则仅落盘，
// 只有总磁盘预算超限才 drop-oldest 段并发 log_upload.gap 标记事件。

import (
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
	bruntime "terminal_bridge/internal/shared/runtime"
	"terminal_bridge/internal/shared/uplink"
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

	// 自举配置：云端（cluster）桥没有浏览器下发令牌，只能靠部署期注入。
	envLogUploadEndpoint = "HCI_BRIDGE_LOG_UPLOAD_ENDPOINT"
	envLogUploadToken    = "HCI_BRIDGE_LOG_UPLOAD_TOKEN"
	// 崩溃补传：启动时把本地日志文件里"水位之后"的条目回灌队列。
	// L1 后这两个常量不再是硬窗口，而是**单轮**预算：扫全量文件（含轮转件与当前文件），
	// 预算用尽记进度、下轮继续，直到水位之后的历史全部进管道。
	DefaultSeedMaxFiles   = 2
	DefaultSeedMaxEntries = 5000
	// 单轮补传的字节/时间预算：启动与 worker 续扫都不得阻塞业务太久。
	seedByteBudget = 8 << 20
	seedTimeBudget = 2 * time.Second
	// 内存队列上限（实时与补传各自独立计数）：溢出部分留在磁盘等 worker 消费（L2）。
	memoryQueueCap = 5000
	// 单个 outbox 分段大小：到顶就滚新段，drop-oldest 与游标消费都以段为单位。
	outboxSegmentBytes = 1 << 20
)

// logUploader 是进程级单例：**串行**发送，全局并发恒为 1。
//
// 为什么必须串行：断点语义依赖"前一批成功才推进"，并发会让失败批次与成功批次
// 交错，重试边界不可判定；在低配机器上并发也只会放大内存与调度开销。
type logUploader struct {
	Mu sync.Mutex
	// sendMu 串行化"取批次 → 发送 → 出队"整段：只靠 mu 保护队列会出两个问题——
	// 两个 goroutine 可能取到同一批（重复发送），且后完成者用旧 size 切新队列（越界）。
	sendMu   sync.Mutex
	token    string
	endpoint string
	CaseID   string
	instance string
	// source 记录令牌来源（env / ws），排障时先看它判断"直传到底有没有被启用"。
	source string
	// queue 是实时队列：与分段 outbox 的磁盘游标一一对应（每条都已落盘），
	// 批次成功后游标按字节前进；重启后从游标 refill，不再整文件读回清空。
	queue []LogEntry
	// seedQueue 是补传队列（本地日志 seed + stderr 包装）：源头在本地日志文件而非 outbox，
	// 不占磁盘游标；内部按 seq 升序（stderr 条目 Seq=0 排在尾部）。
	seedQueue []LogEntry
	// maxQueuedSeq 是本进程已入管道的最大 seq：seed 去重用，避免把已实时入队的条目重复补传。
	maxQueuedSeq uint64
	// maxStderrQueued 是本进程已入管道的最大 stderr 字节偏移（字节水位只在上传成功后推进，
	// 进程内去重靠它）。
	maxStderrQueued int64
	outbox          *os.File
	outboxAt        string
	// gapsFile 是 gap 标记事件的独立落盘文件（gaps.jsonl，不在分段列表内）：
	// gap 不占磁盘预算、不会被 drop-oldest，也不占磁盘游标字节（flush 时扣除）。
	gapsFile *os.File
	// 丢失判定三件套：totalEnqueued 累计入管道条数，consumedEntries 累计已确认发送条数，
	// segEntryIndex 记录各段首条的全局索引。内存覆盖窗口 = 未消费的前 memoryQueueCap 条
	// （它们仍在内存/可 refill，终会发出）；drop-oldest 只把窗口之外的条目计为真丢失并发 gap。
	totalEnqueued   uint64
	consumedEntries uint64
	segEntryIndex   map[string]uint64
	maxBytes        int64
	// segmentBytes 是单个 outbox 分段的大小上限（测试可调小以验证滚段/缺口逻辑）。
	segmentBytes int64
	written      int64
	dropped      uint64
	// gapsReported 是产生的缺口标记数（drop-oldest 与重放合并计），
	// replayed 是重启后从 gaps.jsonl 重放的条数；两者用于现场判断"洞是否真的报出去了"。
	gapsReported uint64
	replayed     uint64
	// spilled 统计"内存满、仅落盘"的条数（L2 后不再丢弃，只是慢一拍）。
	spilled uint64
	// persistOK 标记落盘链路是否健康：任何一次写失败后置 false（refill 暂停、
	// 游标停止前进，避免把同一批数据无限重放）；下一次写成功即复位，复位时由
	// markPersistedLocked 把游标钳回本条起点，保证游标不会越过磁盘上不存在的空洞。
	persistOK bool
	// seedMu 串行化本地补传扫描：configure（env/WS 两条路径）与 worker 每轮都会触发，
	// 并发跑会重复入队，并让进度文件的 tmp 写入互相覆盖（可能读到半截 JSON 导致全量重扫）。
	seedMu sync.Mutex
	// 预算检查节流：每条日志都 ReadDir + Stat 全段会放大 publish 热路径 IO。
	// 累计写入字节达到 maxBytes/8（下限 1KB）或距上次检查 5s 才真正扫描一次。
	budgetCheckBytes int64
	lastBudgetAt     time.Time
	started          bool
	wake             chan struct{}
	attempt          int
	// relaxed 记录"已确认平台使用自签证书、允许跳过校验"的 endpoint。
	//
	// 只记忆进程生命周期：重启后第一次会多一次握手失败，随后自愈。
	// 落盘没有必要——放宽的判据是"这个 endpoint 的证书不受信"，
	// 每次重新判定比持久化一份信任决定更安全（CA 换了能自动回到严格模式）。
	relaxed map[string]bool
	// syncFailedLogged 让 fsync 失败只留痕一次：它可能每轮都失败，
	// 逐轮打印会把真正的问题淹掉。
	syncFailedLogged bool
	// unsyncedBytes 是活动段自上次 fsync 以来新写入的字节数。
	// 为 0 时跳过 fsync：没有脏数据还调 Sync 只是白付一次系统调用与日志提交，
	// 在慢盘客户机上会无谓地拉长持锁时间。
	unsyncedBytes int64
}

var LogUpload = newLogUploader()

func newLogUploader() *logUploader {
	return &logUploader{
		wake:          make(chan struct{}, 1),
		maxBytes:      int64(bruntime.EnvIntOrDefault("HCI_BRIDGE_LOG_OUTBOX_MAX_BYTES", defaultLogOutboxMaxBytes)),
		segmentBytes:  outboxSegmentBytes,
		relaxed:       make(map[string]bool),
		persistOK:     true,
		segEntryIndex: make(map[string]uint64),
	}
}

// configure 接收前端下发的直传凭据并启动 worker。
//
// 幂等：同一 token 重复下发不会重启 worker；token 变化时重置退避计数，
// 让上一次失败（例如旧令牌过期）不拖累新令牌。
func (u *logUploader) Configure(token, endpoint, caseID, instance string) {
	u.configureWithSource(token, endpoint, caseID, instance, "ws")
}

// configureWithSource 与 configure 同一语义，但显式记录凭据来源。
func (u *logUploader) configureWithSource(token, endpoint, caseID, instance, source string) {
	if strings.TrimSpace(token) == "" || strings.TrimSpace(endpoint) == "" {
		return
	}
	u.Mu.Lock()
	changed := u.token != token
	u.token = strings.TrimSpace(token)
	u.endpoint = strings.TrimSpace(endpoint)
	u.CaseID = strings.TrimSpace(caseID)
	u.instance = strings.TrimSpace(instance)
	u.source = strings.TrimSpace(source)
	if changed {
		u.attempt = 0
	}
	needStart := !u.started
	u.started = true
	u.Mu.Unlock()

	u.recoverOutbox()

	Emit("INFO", "log_upload.configured", "桥日志直传已启用", "", caseID, "", "", map[string]any{
		"endpoint": endpoint, "instance": instance, "token_changed": changed, "source": source,
	})
	if needStart {
		go u.run()
	}
	// L1：令牌到手即补"水位之后"的全部本地历史（含令牌启用前的旧历史）。
	// 异步执行：历史可能很大，预算内分轮续扫，不阻塞令牌 ack 与启动；
	// env 旋钮保留，解释为单轮预算（文件数不再限制扫描范围）。
	go u.SeedFromLocalLogs(
		bruntime.EnvIntOrDefault("HCI_BRIDGE_LOG_SEED_FILES", DefaultSeedMaxFiles),
		bruntime.EnvIntOrDefault("HCI_BRIDGE_LOG_SEED_ENTRIES", DefaultSeedMaxEntries),
	)
	u.notify()
}

// bootstrapFromEnv 用部署期注入的 endpoint + 令牌自建直传通道。
//
// 为什么必须有这条路：现有唯一入口是浏览器经 WS 下发 `bridge_log_token`，
// 而生产环境的桥部署在云端，没有浏览器给它发令牌 —— `/status` 长期显示
// `log_upload.enabled=false`，服务端直连桥发起的插件下发日志一条也进不了库。
// 部署期注入把"是否上传"从依赖页面在线，改成依赖部署配置。
func (u *logUploader) BootstrapFromEnv() bool {
	endpoint := strings.TrimSpace(os.Getenv(envLogUploadEndpoint))
	token := strings.TrimSpace(os.Getenv(envLogUploadToken))
	if endpoint == "" || token == "" {
		return false
	}
	u.configureWithSource(token, endpoint, "", DefaultHub.InstanceID, "env")
	return u.enabled()
}

// enabled 返回当前是否具备直传条件（有令牌且已配置）。
func (u *logUploader) enabled() bool {
	u.Mu.Lock()
	defer u.Mu.Unlock()
	return u.token != "" && u.endpoint != ""
}

// enqueue 把一条事件放进发送队列并落盘。
//
// 调用点必须在 DefaultHub 解锁之后（enqueue 自己加锁，避免与 publish 的锁嵌套）。
// L2：内存队列满不再丢弃——条目仍落盘（spill），等 worker 从磁盘游标 refill 消费；
// 只有总磁盘预算超限才 drop-oldest 段（在 persistLocked 内执行）并发 gap 标记事件。
func (u *logUploader) enqueue(entry LogEntry) {
	if !u.enabled() {
		return
	}
	u.Mu.Lock()
	gaps, outcome := u.persistLocked(entry)
	u.totalEnqueued++
	queued := len(u.queue) < memoryQueueCap
	if queued {
		u.queue = append(u.queue, entry)
		if entry.Seq > u.maxQueuedSeq {
			u.maxQueuedSeq = entry.Seq
		}
	} else {
		u.spilled++
	}
	// 真丢判定：既进不了内存队列、又没落盘。必须计数并留痕，否则"零静默丢弃"
	// 在磁盘满（写失败）+ 队列满时会变成静默丢弃——此时平台与本地都无从得知。
	if !queued && outcome == persistFailed {
		u.dropped++
		u.reportLostLocked(entry)
	}
	// gap 标记已写独立文件，内存按余量入队（紧跟触发条目，与段生命周期对齐）。
	for _, gap := range gaps {
		if len(u.queue) < memoryQueueCap {
			u.queue = append(u.queue, gap)
		} else {
			u.spilled++
		}
	}
	u.Mu.Unlock()
	u.notify()
}

// reportLostLocked 记录一条真实丢失（落盘失败且内存队列已满）。调用方持有 u.Mu。
//
// 为什么走 stderr 而不是 Emit：Emit 会经 publish 回到 enqueue 重入 u.Mu（自死锁）。
// stderr 在桌面形态被 redirectStderrToFile 落进 bridge-stderr.log，由 L3 补传通道
// 包装上报，因此"平台可知洞"仍然成立。按首条 + 每 100 条节流，避免满盘时刷爆文件。
func (u *logUploader) reportLostLocked(entry LogEntry) {
	if u.dropped != 1 && u.dropped%100 != 0 {
		return
	}
	fmt.Fprintf(os.Stderr, "log_upload.lost event=%s seq=%d reason=outbox_write_failed_and_queue_full dropped_total=%d\n",
		entry.Event, entry.Seq, u.dropped)
}

// notify 非阻塞唤醒 worker。
func (u *logUploader) notify() {
	select {
	case u.wake <- struct{}{}:
	default:
	}
}

func (u *logUploader) interval() time.Duration {
	seconds := bruntime.EnvIntOrDefault("HCI_BRIDGE_LOG_UPLOAD_INTERVAL_SECONDS", int(defaultLogUploadInterval/time.Second))
	if seconds <= 0 {
		seconds = 2
	}
	return time.Duration(seconds) * time.Second
}

func (u *logUploader) batchSize() int {
	size := bruntime.EnvIntOrDefault("HCI_BRIDGE_LOG_UPLOAD_BATCH", defaultLogUploadBatch)
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
				Emit("WARN", "log_upload.failed", "桥日志直传失败，稍后重试", "", "", "", "", map[string]any{
					"error": err.Error(), "backoff_seconds": int(delay.Seconds()), "queued": u.queueLen(),
				})
				time.Sleep(delay)
				break
			}
			if sent == 0 {
				break
			}
		}
		// L1：补传预算用尽后续扫；当前文件有新内容（崩溃窗口/令牌前条目）时也重扫。
		// 水位 + maxQueuedSeq 双重去重，重扫不会重复入队。
		if u.seedPending() {
			u.SeedFromLocalLogs(0, 0)
		}
	}
}

func (u *logUploader) queueLen() int {
	u.Mu.Lock()
	defer u.Mu.Unlock()
	return len(u.queue) + len(u.seedQueue)
}

// backoff 返回下一次重试延迟：1s→2s→…→60s。
func (u *logUploader) backoff() time.Duration {
	u.Mu.Lock()
	defer u.Mu.Unlock()
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
// 消费顺序：先补传队列（旧历史优先），再实时队列；实时队列空且磁盘有溢出时从游标 refill。
func (u *logUploader) flushOnce() (int, error) {
	// 串行化整段：worker 与手动触发（含测试/运维）并发时，不得有两条同时取批次。
	u.sendMu.Lock()
	defer u.sendMu.Unlock()

	u.Mu.Lock()
	if u.token == "" || u.endpoint == "" {
		u.Mu.Unlock()
		return 0, nil
	}
	fromSeed := len(u.seedQueue) > 0
	if !fromSeed && len(u.queue) == 0 {
		// L2：内存队列空不代表发完——溢出部分还在磁盘，从游标 refill 后再取批。
		u.refillFromDiskLocked()
	}
	source := u.queue
	if fromSeed {
		source = u.seedQueue
	}
	if len(source) == 0 {
		u.Mu.Unlock()
		return 0, nil
	}
	size := u.batchSize()
	if size > len(source) {
		size = len(source)
	}
	batch := make([]LogEntry, size)
	copy(batch, source[:size])
	// 发送前把活动段刷盘（P1-3）：游标只在本批确认成功后前进，先落盘才能保证
	// 「游标已越过」与「数据确实在磁盘」一致；断电时最多丢最近一轮尚未刷盘的尾部。
	u.syncOutboxLocked()
	token, endpoint := u.token, u.endpoint
	u.Mu.Unlock()

	payload, err := encodeLogBatch(batch)
	if err != nil {
		return 0, err
	}
	response, err := u.sendBatch(payload, endpoint, token)
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
	maxSeq := uint64(0)
	maxStderr := int64(0)
	consumedBytes := int64(0)
	for _, entry := range batch {
		if entry.Seq > maxSeq {
			maxSeq = entry.Seq
		}
		if entry.StderrOffset > maxStderr {
			maxStderr = entry.StderrOffset
		}
		if !fromSeed {
			if line, marshalErr := json.Marshal(entry); marshalErr == nil {
				// gap 条目落在独立文件，不占分段游标字节，必须扣除。
				if entry.Event != "log_upload.gap" {
					consumedBytes += int64(len(line)) + 1
				}
			}
		}
	}
	u.Mu.Lock()
	if fromSeed {
		u.seedQueue = u.seedQueue[size:]
	} else {
		u.queue = u.queue[size:]
		u.consumedEntries += uint64(size)
		// 磁盘游标与实时队列一一对应：批次确认成功后才按字节前进（落盘链路健康时）。
		if u.persistOK && consumedBytes > 0 {
			u.advanceCursorLocked(consumedBytes)
		}
	}
	u.attempt = 0
	u.Mu.Unlock()
	// seq 水位只在实时批次确认成功后推进：补传批次的低 seq 若推水位，
	// 会把尚未发送的高 seq 实时条目跳过，崩溃后永久丢失。
	if !fromSeed && maxSeq > 0 {
		u.saveWatermark(maxSeq)
	}
	// stderr 字节水位：panic 现场只有确认送达后才算补完，重启后从水位续读。
	if maxStderr > 0 {
		u.saveStderrWatermark(maxStderr)
	}
	return size, nil
}

// sendBatch 发送一批日志；平台使用自签证书时自动放宽一次并记住该 endpoint。
//
// 为什么必须自动放宽：客户内网平台普遍自签，客户机的系统根证书库不可能预置这份 CA，
// 严格校验下这条通道**永远**发不出去。现场（2026-10-27）表现是每批日志都失败、
// 退避一路涨到 16 秒、队列积压到 28 条，最终交付出问题时平台侧一条桥日志都没有，
// 排查只能人工拷贝客户机终端。
//
// 安全边界：这条通道只写绑定工单的日志、只携带只写令牌，放宽 TLS 不等于放宽鉴权；
// 放宽范围被限定在**该 endpoint**，不影响交付通道，也不影响其它域名。
func (u *logUploader) sendBatch(payload []byte, endpoint, token string) (*http.Response, error) {
	u.Mu.Lock()
	instance := u.instance
	u.Mu.Unlock()
	build := func() (*http.Request, error) {
		request, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(payload))
		if err != nil {
			return nil, err
		}
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Content-Encoding", "gzip")
		request.Header.Set("X-Bridge-Upload-Token", token)
		// 实例号必须走请求头：条目里的实例号可能为空（历史事件），
		// 服务端要靠它把"云端桥的内部令牌"绑定到具体实例，防止令牌被复制滥用。
		if instance != "" {
			request.Header.Set("X-Bridge-Instance-Id", instance)
		}
		return request, nil
	}

	relaxed := u.isTLSRelaxed(endpoint)
	request, err := build()
	if err != nil {
		return nil, err
	}
	response, err := uplink.HttpClientFor(relaxed, logUploadTimeout).Do(request)
	if err == nil || relaxed || !uplink.IsUntrustedCertificateError(err) {
		return response, err
	}

	// 命中自签证书：放宽一次重试，并记住该 endpoint，避免每一批都先失败一轮。
	u.markTLSRelaxed(endpoint)
	Emit("WARN", "log_upload.tls_relaxed", "平台证书不受信，桥日志直传已放宽证书校验", "", "", "", "", map[string]any{
		"endpoint": endpoint, "reason": "self-signed-certificate",
	})
	retry, buildErr := build()
	if buildErr != nil {
		return nil, err
	}
	return uplink.HttpClientFor(true, logUploadTimeout).Do(retry)
}

func (u *logUploader) isTLSRelaxed(endpoint string) bool {
	u.Mu.Lock()
	defer u.Mu.Unlock()
	return u.relaxed[endpoint]
}

func (u *logUploader) markTLSRelaxed(endpoint string) {
	u.Mu.Lock()
	defer u.Mu.Unlock()
	if u.relaxed == nil {
		u.relaxed = make(map[string]bool)
	}
	u.relaxed[endpoint] = true
}

// encodeLogBatch 把事件编码为 gzip 压缩的 JSON。
func encodeLogBatch(batch []LogEntry) ([]byte, error) {
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
	dir, _ := hubLogLocation()
	if dir == "" {
		return ""
	}
	return filepath.Join(dir, "bridge-log-outbox")
}

// LogUploadStats 供 /status 暴露直传状态（排障时无需再猜是否启用）。
func LogUploadStats() map[string]any {
	u := LogUpload
	u.Mu.Lock()
	defer u.Mu.Unlock()
	stats := map[string]any{
		"enabled": u.token != "" && u.endpoint != "",
		"queued":  len(u.queue) + len(u.seedQueue),
		// dropped 与写入侧（持 u.Mu 时 u.dropped++）用同一把锁读写；
		// 混用 atomic.Load 与互斥写不构成 happens-before，是数据竞争。
		"dropped":      u.dropped,
		"spilled":      u.spilled,
		"gaps":         u.gapsReported,
		"replayed":     u.replayed,
		"seed_pending": len(u.seedQueue),
		"instance":     u.instance,
		"outbox":       u.outboxAt,
		// persist_ok=false 是现场最重要的一个信号：落盘链路已坏（磁盘满/目录不可写），
		// refill 已暂停、游标已冻结；此时 dropped 增长代表真实丢失，需立即清理磁盘。
		"persist_ok": u.persistOK,
		// source 让排障第一眼就能区分"部署期注入生效"与"只有浏览器下发"。
		"source": u.source,
	}
	// 游标是判断"投递是否真的在推进"的硬指标：偏移长时间不变 = 平台不可达或鉴权失败。
	if dir := outboxDir(); dir != "" && u.instance != "" {
		cursor := loadOutboxCursor(outboxInstanceDir(dir, u.instance))
		stats["cursor_segment"] = cursor.Segment
		stats["cursor_offset"] = cursor.Offset
	}
	return stats
}
