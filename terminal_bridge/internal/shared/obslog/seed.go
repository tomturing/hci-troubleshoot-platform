package obslog

// L1/L3/L4 补传引擎：目标 = 本地保留窗口内条目至少一次必达平台（设计 §3.6）。
//
// L1 水位全量补传：废除"2 文件/5000 条"固定窗口，扫水位之后的**全部**日志文件，
// 单轮受字节/时间/条数预算约束，预算用尽把进度（文件+字节偏移）落盘、下轮继续。
// L4 当前文件纳入：不再 exclude 正在写的文件，读到最后一个完整行为止；
// 轮转件 bridge-*.log.1 同样纳入（旧实现只匹配 .log 后缀，轮转件永远进不了窗口）。
// L3 stderr 包装：bridge-stderr.log 的非 JSON 行包装为 bridge.stderr/ERROR 条目，
// 水位按字节偏移（非 seq），panic 现场在重启后必达平台。
//
// 去重三层：seq 水位（已确认上传）+ maxQueuedSeq（本进程已入管道）+ 后端 event_id 幂等。
// 补传条目进 seedQueue（仅内存，不占 outbox 磁盘游标），seq 水位不随补传批次推进。

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	bruntime "terminal_bridge/internal/shared/runtime"
	"time"
)

// hubLogLocationMu 保护 DefaultHub.LogDir/LogPath 的读侧快照：生产里两者启动期
// 一次性设定，但测试缝（withLogDir）会并发改写，后台 seed/worker 不得裸读。
var hubLogLocationMu sync.RWMutex

// hubLogLocation 返回日志目录与当前文件路径的一致性快照。
func hubLogLocation() (string, string) {
	hubLogLocationMu.RLock()
	defer hubLogLocationMu.RUnlock()
	return strings.TrimSpace(DefaultHub.LogDir), strings.TrimSpace(DefaultHub.LogPath)
}

// seedProgress 是补传扫描的持久化进度：预算用尽时记下"扫到哪个文件的哪个字节"，
// 下轮从断点继续；整轮扫完后记录 DoneBefore（此前修改的文件已扫完，跳过）与
// 当前文件偏移/大小（下轮只读新增尾部，避免每 2 秒重读整个当前文件）。
type seedProgress struct {
	Path        string `json:"path"`
	Offset      int64  `json:"offset"`
	DoneBefore  int64  `json:"done_before"`
	CurrentSize int64  `json:"current_size"`
	Pending     bool   `json:"pending"`
}

// SeedFromLocalLogs 把本地日志文件里尚未确认上传的条目回灌补传队列，返回本轮补传条数。
//
// 签名保留 maxFiles/maxEntries 兼容既有调用点（main.go env 注入）：L1 后 maxFiles 不再
// 限制扫描范围（恒扫全量），maxEntries 解释为**单轮条数预算**（<=0 取默认 5000）。
// 覆盖的场景：进程被 kill / OOM / Pod 重建、令牌启用前的旧历史、panic 现场。
func (u *logUploader) SeedFromLocalLogs(maxFiles, maxEntries int) int {
	// 串行化补传扫描：configure（env/WS 两条路径，各自 go 出去）与 worker 每轮都会触发，
	// 并发跑会重复入队（浪费带宽），并让进度文件的 tmp 写入互相覆盖——后者可能读到
	// 半截 JSON，被当成"无进度"而全量重扫。
	u.seedMu.Lock()
	defer u.seedMu.Unlock()
	if !u.enabled() {
		return 0
	}
	dir, current := hubLogLocation()
	if dir == "" {
		return 0
	}
	entryBudget := maxEntries
	if entryBudget <= 0 {
		entryBudget = DefaultSeedMaxEntries
	}
	watermark := u.loadWatermark()
	progress := u.loadSeedProgress()
	files := bridgeLogFilesForSeed(dir)

	// 断点续扫：上一轮停在哪个文件就从哪个文件继续；文件已消失（轮转删除）则从头扫。
	start, resumeOffset := 0, int64(0)
	if progress.Path != "" {
		for i, f := range files {
			if f.path == progress.Path {
				start, resumeOffset = i, progress.Offset
				break
			}
		}
	}

	bytesLeft := int64(seedByteBudget)
	deadline := time.Now().Add(seedTimeBudget)
	seeded := 0
	currentEnd := int64(0)
	for i := start; i < len(files); i++ {
		f := files[i]
		// 已扫完的旧文件跳过；当前文件永远重扫（只读新增尾部）。
		if f.path != current && progress.DoneBefore > 0 && f.mod.UnixNano() < progress.DoneBefore {
			continue
		}
		offset := int64(0)
		if f.path == progress.Path {
			offset = resumeOffset
		}
		n, next, exhausted := u.scanLogFile(f.path, offset, watermark, entryBudget-seeded, &bytesLeft, deadline)
		seeded += n
		if f.path == current {
			currentEnd = next
		}
		if exhausted {
			u.saveSeedProgress(seedProgress{Path: f.path, Offset: next, DoneBefore: progress.DoneBefore, CurrentSize: progress.CurrentSize, Pending: true})
			u.logSeeded(seeded, watermark, true)
			u.notify()
			return seeded
		}
	}

	// L3：日志文件扫完后用剩余预算扫 stderr（panic/runtime fatal 的唯一出口）。
	n, exhausted := u.scanStderrFile(dir, entryBudget-seeded, &bytesLeft, deadline)
	seeded += n
	if exhausted {
		u.saveSeedProgress(seedProgress{DoneBefore: progress.DoneBefore, CurrentSize: progress.CurrentSize, Pending: true})
		u.logSeeded(seeded, watermark, true)
		u.notify()
		return seeded
	}

	// 整轮完成：记录"此前文件已扫完"+当前文件断点，下轮只读增量。
	u.saveSeedProgress(seedProgress{Path: current, Offset: currentEnd, DoneBefore: time.Now().UnixNano(), CurrentSize: fileSize(current), Pending: false})
	u.logSeeded(seeded, watermark, false)
	if seeded > 0 {
		u.notify()
	}
	return seeded
}

func (u *logUploader) logSeeded(seeded int, watermark uint64, pending bool) {
	if seeded == 0 && !pending {
		return
	}
	Emit("INFO", "log_upload.seeded", "已从本地日志文件补传未上传区间", "", "", "", "", map[string]any{
		"seeded": seeded, "watermark": watermark, "pending": pending,
	})
}

// seedPending 判断是否还有补传工作：上轮预算用尽、当前文件有新增、stderr 有未入管道的新行。
func (u *logUploader) seedPending() bool {
	if !u.enabled() {
		return false
	}
	dir, current := hubLogLocation()
	if dir == "" {
		return false
	}
	if u.loadSeedProgress().Pending {
		return true
	}
	if current != "" {
		if info, err := os.Stat(current); err == nil && info.Size() > u.loadSeedProgress().CurrentSize {
			return true
		}
	}
	if info, err := os.Stat(filepath.Join(dir, "bridge-stderr.log")); err == nil {
		u.Mu.Lock()
		queued := u.maxStderrQueued
		u.Mu.Unlock()
		if info.Size() > queued {
			return true
		}
	}
	return false
}

// scanLogFile 从 offset 起读一个日志文件，把水位之后且未入管道的条目并入 seedQueue。
// 返回（本轮补传条数, 停止字节偏移, 是否预算用尽）。只消费以 \n 结尾的完整行（L4）。
func (u *logUploader) scanLogFile(path string, offset int64, watermark uint64, entryBudget int, bytesLeft *int64, deadline time.Time) (int, int64, bool) {
	file, err := os.Open(path)
	if err != nil {
		return 0, offset, false
	}
	defer file.Close()
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return 0, offset, false
	}
	reader := bufio.NewReaderSize(file, 64*1024)
	pos, seeded := offset, 0
	// 批量收集后再合并：逐条插入在 5000 条补传时是 O(n²)（P1-6）。
	// 用 defer 兜底，保证预算用尽/不完整行/队列满等提前返回时，
	// 已入账（maxQueuedSeq 已推进）的条目不会滞留在局部变量里丢失。
	var pending []LogEntry
	flushPending := func() {
		if len(pending) == 0 {
			return
		}
		u.Mu.Lock()
		u.mergeSeedBatchLocked(pending)
		u.Mu.Unlock()
		pending = pending[:0]
	}
	defer flushPending()
	for {
		if *bytesLeft <= 0 || entryBudget <= 0 || time.Now().After(deadline) {
			return seeded, pos, true
		}
		u.Mu.Lock()
		full := len(u.seedQueue) >= memoryQueueCap
		u.Mu.Unlock()
		if full {
			// 补传队列也受内存上限约束：满了就停，等 worker 消费后下轮继续。
			return seeded, pos, true
		}
		line, readErr := reader.ReadBytes('\n')
		if readErr != nil {
			// 尾部不完整行不消费（进程可能正在写），下次从 pos 重读。
			return seeded, pos, false
		}
		pos += int64(len(line))
		*bytesLeft -= int64(len(line))
		var entry LogEntry
		if err := json.Unmarshal(line, &entry); err != nil {
			continue
		}
		if entry.Seq <= watermark || entry.EventID == "" {
			continue
		}
		needFlush := false
		u.Mu.Lock()
		if entry.Seq > u.maxQueuedSeq {
			pending = append(pending, entry)
			u.maxQueuedSeq = entry.Seq
			seeded++
			entryBudget--
			needFlush = len(pending) >= seedMergeBatchSize
		}
		u.Mu.Unlock()
		if needFlush {
			flushPending()
		}
	}
}

// seedMergeBatchSize 是一次批量合并的条数上限。
//
// 太小会退化成逐条插入（回到 O(n²)）；太大则补传队列长时间落后于扫描进度，
// worker 读不到刚扫到的条目，补传表现为"扫完了但不发"。
const seedMergeBatchSize = 512

// mergeSeedBatchLocked 把一批补传条目并入 seedQueue，整体保持 seq 升序
// （stderr 条目 Seq=0 恒排尾部）。
//
// 为什么改成批量：原实现逐条 sort.Search + copy，5000 条补传时是 O(n²)，
// 扫描预算被插入动作吃掉，补传进度上不去（P1-6）。批量后每 512 条一次
// 全量稳定排序，总量降到 O(n log n)。
//
// 正确性：入队条件 entry.Seq > maxQueuedSeq 保证本批 seq 严格递增且大于
// 既有队列的实时条目，因此排序结果与逐条插入完全一致；SliceStable 让
// Seq=0 条目之间的相对顺序保持不变（与旧实现的"直接 append 到尾部"等价）。
func (u *logUploader) mergeSeedBatchLocked(entries []LogEntry) {
	if len(entries) == 0 {
		return
	}
	u.seedQueue = append(u.seedQueue, entries...)
	sort.SliceStable(u.seedQueue, func(i, j int) bool {
		a, b := u.seedQueue[i].Seq, u.seedQueue[j].Seq
		// Seq=0（stderr 包装）没有 seq 语义，恒排尾部。
		if a == 0 {
			return false
		}
		if b == 0 {
			return true
		}
		return a < b
	})
}

// scanStderrFile 从字节水位起读 bridge-stderr.log，把非 JSON 行包装为 bridge.stderr/ERROR。
// 返回（补传条数, 是否预算用尽）。字节水位只在上传成功后推进（flushOnce），
// 进程内去重靠 maxStderrQueued，重启后从持久化水位续读。
// stderrGenFileName 与 app.RedirectStderrToFile 的轮转标记保持一致：
// 每次轮转写一个新的纳秒时间戳，作为该代 stderr 文件的身份。
const stderrGenFileName = "bridge-stderr.gen"

// stderrGeneration 读取 stderr 轮转代数；不存在或为空时返回 "0"（兼容轮转引入前的旧现场）。
func stderrGeneration(dir string) string {
	data, err := os.ReadFile(filepath.Join(dir, stderrGenFileName))
	if err != nil {
		return "0"
	}
	gen := strings.TrimSpace(string(data))
	if gen == "" {
		return "0"
	}
	return gen
}

func (u *logUploader) scanStderrFile(dir string, entryBudget int, bytesLeft *int64, deadline time.Time) (int, bool) {
	path := filepath.Join(dir, "bridge-stderr.log")
	file, err := os.Open(path)
	if err != nil {
		return 0, false
	}
	defer file.Close()
	watermark := u.loadStderrWatermark()
	if _, err := file.Seek(watermark, io.SeekStart); err != nil {
		return 0, false
	}
	reader := bufio.NewReaderSize(file, 64*1024)
	pos, seeded := watermark, 0
	for {
		if *bytesLeft <= 0 || entryBudget <= 0 || time.Now().After(deadline) {
			return seeded, true
		}
		u.Mu.Lock()
		full := len(u.seedQueue) >= memoryQueueCap
		queued := u.maxStderrQueued
		instance := u.instance
		u.Mu.Unlock()
		if full {
			return seeded, true
		}
		line, readErr := reader.ReadBytes('\n')
		if readErr != nil {
			return seeded, false
		}
		end := pos + int64(len(line))
		pos = end
		*bytesLeft -= int64(len(line))
		text := strings.TrimSpace(string(line))
		// JSON 行不是 panic 现场（可能是误导入的结构化输出），空行无证据价值。
		if text == "" || strings.HasPrefix(text, "{") || end <= queued {
			continue
		}
		entry := LogEntry{
			Type:      "bridge_log",
			Level:     "ERROR",
			Service:   "terminal_bridge",
			Event:     "bridge.stderr",
			Message:   text,
			Timestamp: time.Now().UTC().Format(time.RFC3339Nano),
			// 带轮转代数：轮转后偏移从 0 重新计数，不带代数会与历史同一偏移的
			// event_id 碰撞，被平台按幂等规则误判为重复而丢弃新现场。
			EventID:               deterministicUUID(fmt.Sprintf("stderr:%s:%s:%d", instance, stderrGeneration(dir), end)),
			BridgeInstanceID:      instance,
			ServiceName:           "terminal-bridge",
			ServiceVersion:        serviceVersion(),
			DeploymentEnvironment: bruntime.EnvOrDefault("HCI_DEPLOYMENT_ENVIRONMENT", "local"),
			StderrOffset:          end,
		}
		u.Mu.Lock()
		u.seedQueue = append(u.seedQueue, entry)
		u.maxStderrQueued = end
		u.Mu.Unlock()
		seeded++
		entryBudget--
	}
}

// bridgeLogFilesForSeed 列出目录内全部 bridge 日志文件（含轮转件 .log.1 与当前文件），
// 按修改时间旧→新排序。bridge-stderr.log 走独立的字节水位通道，必须排除。
func bridgeLogFilesForSeed(dir string) []seedFile {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	items := make([]seedFile, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasPrefix(name, "bridge-") || name == "bridge-stderr.log" {
			continue
		}
		if !strings.HasSuffix(name, ".log") && !strings.HasSuffix(name, ".log.1") {
			continue
		}
		info, statErr := entry.Info()
		if statErr != nil {
			continue
		}
		items = append(items, seedFile{path: filepath.Join(dir, name), mod: info.ModTime()})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].mod.Before(items[j].mod) })
	return items
}

type seedFile struct {
	path string
	mod  time.Time
}

func fileSize(path string) int64 {
	if path == "" {
		return 0
	}
	if info, err := os.Stat(path); err == nil {
		return info.Size()
	}
	return 0
}

// seedProgressPath / loadSeedProgress / saveSeedProgress：补传进度按实例持久化。
func (u *logUploader) seedProgressPath() string {
	dir, _ := hubLogLocation()
	u.Mu.Lock()
	instance := u.instance
	u.Mu.Unlock()
	if dir == "" || instance == "" {
		return ""
	}
	return filepath.Join(dir, "bridge-log-seed-"+instance+".json")
}

func (u *logUploader) loadSeedProgress() seedProgress {
	var progress seedProgress
	path := u.seedProgressPath()
	if path == "" {
		return progress
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return progress
	}
	_ = json.Unmarshal(raw, &progress)
	return progress
}

func (u *logUploader) saveSeedProgress(progress seedProgress) {
	path := u.seedProgressPath()
	if path == "" {
		return
	}
	raw, err := json.Marshal(progress)
	if err != nil {
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}

// watermarkPath 记录"该实例已成功上传到哪个 seq"，用于重启后精确补传。
func (u *logUploader) watermarkPath() string {
	dir, _ := hubLogLocation()
	u.Mu.Lock()
	instance := u.instance
	u.Mu.Unlock()
	if dir == "" || instance == "" {
		return ""
	}
	return filepath.Join(dir, "bridge-log-uploaded-"+instance+".Seq")
}

func (u *logUploader) loadWatermark() uint64 {
	path := u.watermarkPath()
	if path == "" {
		return 0
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	value, err := strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return 0
	}
	return value
}

func (u *logUploader) saveWatermark(seq uint64) {
	path := u.watermarkPath()
	if path == "" || seq == 0 {
		return
	}
	if current := u.loadWatermark(); seq <= current {
		return
	}
	// 先写临时文件再 rename：进程在写一半时被杀，水位不会变成半截数字。
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(strconv.FormatUint(seq, 10)), 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}

// stderrWatermarkPath / loadStderrWatermark / saveStderrWatermark：L3 字节水位，
// 与 seq 水位同一套"只进不退 + tmp/rename 原子写"纪律。
func (u *logUploader) stderrWatermarkPath() string {
	dir, _ := hubLogLocation()
	u.Mu.Lock()
	instance := u.instance
	u.Mu.Unlock()
	if dir == "" || instance == "" {
		return ""
	}
	return filepath.Join(dir, "bridge-log-uploaded-"+instance+".stderr")
}

func (u *logUploader) loadStderrWatermark() int64 {
	path := u.stderrWatermarkPath()
	if path == "" {
		return 0
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	value, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return 0
	}
	return value
}

func (u *logUploader) saveStderrWatermark(offset int64) {
	path := u.stderrWatermarkPath()
	if path == "" || offset <= 0 {
		return
	}
	if current := u.loadStderrWatermark(); offset <= current {
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(strconv.FormatInt(offset, 10)), 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}
