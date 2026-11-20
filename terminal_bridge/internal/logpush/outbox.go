package logpush

// L2 分段 outbox：溢出落盘代替丢弃（设计 §3.6）。
//
// 旧实现在"队列 ≥5000 或 outbox 超字节上限"时静默丢弃条目；新实现：
//   - 每条实时事件都追加到分段文件（seg-<纳秒>.jsonl，单段 1MB 到顶滚新段）；
//   - 内存队列满时条目仅落盘（spill），worker 空闲时从磁盘游标 refill 消费；
//   - 游标（cursor.json：段名+字节偏移）只在批次确认成功后前进，重启从游标续传；
//   - 仅总磁盘预算（HCI_BRIDGE_LOG_OUTBOX_MAX_BYTES，默认 8MB）超限时 drop-oldest
//     已消费完的段，并发 log_upload.gap 标记事件携带丢弃区间——平台可知洞，不再静默丢失。
//
// 对应关系不变量：实时队列 queue 是"磁盘游标之后未消费数据"的内存前缀，
// 顺序与磁盘逐字节一致；补传队列 seedQueue 不经过 outbox，不占游标。

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	bruntime "terminal_bridge/internal/shared/runtime"
	"time"
)

// outboxCursor 是磁盘消费游标：指向最旧未消费完的段与段内字节偏移。
type outboxCursor struct {
	Segment string `json:"segment"`
	Offset  int64  `json:"offset"`
}

const legacyOutboxSegment = "seg-0000000000000000000.jsonl"

// outboxInstanceDir 返回实例的分段目录：bridge-log-outbox/<instance>/。
func outboxInstanceDir(dir, instance string) string {
	if dir == "" || instance == "" {
		return ""
	}
	return filepath.Join(dir, instance)
}

func listOutboxSegments(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if !entry.IsDir() && strings.HasPrefix(name, "seg-") && strings.HasSuffix(name, ".jsonl") {
			names = append(names, name)
		}
	}
	// 段名含零填充纳秒时间戳，字典序即时间序（旧→新）。
	sort.Strings(names)
	return names
}

func loadOutboxCursor(dir string) outboxCursor {
	var cursor outboxCursor
	raw, err := os.ReadFile(filepath.Join(dir, "cursor.json"))
	if err != nil {
		return cursor
	}
	_ = json.Unmarshal(raw, &cursor)
	return cursor
}

func saveOutboxCursor(dir string, cursor outboxCursor) {
	raw, err := json.Marshal(cursor)
	if err != nil {
		return
	}
	tmp := filepath.Join(dir, "cursor.json.tmp")
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, filepath.Join(dir, "cursor.json"))
}

// migrateLegacyOutbox 把旧版单文件 outbox（<instance>.jsonl）迁为最旧分段。
// 只在游标尚未建立时迁移，避免把旧文件插到已消费位置之前造成漏传。
func migrateLegacyOutbox(dir, instance string) {
	legacy := filepath.Join(dir, instance+".jsonl")
	if _, err := os.Stat(legacy); err != nil {
		return
	}
	instDir := outboxInstanceDir(dir, instance)
	if instDir == "" {
		return
	}
	if loadOutboxCursor(instDir).Segment != "" {
		return
	}
	if err := os.MkdirAll(instDir, 0o700); err != nil {
		return
	}
	_ = os.Rename(legacy, filepath.Join(instDir, legacyOutboxSegment))
}

// persistLocked 追加一条到活动分段；返回磁盘预算触发的 gap 标记事件（可为空）。
// 调用方持有 u.Mu。写失败置 persistOK=false：游标停止前进，重复交给后端幂等。
func (u *logUploader) persistLocked(entry LogEntry) []LogEntry {
	dir := outboxDir()
	if dir == "" || u.instance == "" {
		return nil
	}
	instDir := outboxInstanceDir(dir, u.instance)
	if u.outbox == nil {
		if err := os.MkdirAll(instDir, 0o700); err != nil {
			u.persistOK = false
			return nil
		}
		// 续接最新段（游标可能还在更旧的段上消费，追加不影响顺序）。
		segments := listOutboxSegments(instDir)
		name := fmt.Sprintf("seg-%019d.jsonl", time.Now().UnixNano())
		if len(segments) > 0 {
			name = segments[len(segments)-1]
		}
		path := filepath.Join(instDir, name)
		file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			u.persistOK = false
			return nil
		}
		u.outbox, u.outboxAt = file, path
		if info, statErr := file.Stat(); statErr == nil {
			u.written = info.Size()
		}
		if u.segEntryIndex == nil {
			u.segEntryIndex = make(map[string]uint64)
		}
		if _, exists := u.segEntryIndex[name]; !exists {
			u.segEntryIndex[name] = u.totalEnqueued
		}
	}
	// 单段到顶滚新段：drop-oldest 与游标消费都以段为单位。
	if u.written >= u.segmentBytes {
		_ = u.outbox.Close()
		u.outbox = nil
		name := fmt.Sprintf("seg-%019d.jsonl", time.Now().UnixNano())
		path := filepath.Join(instDir, name)
		file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			u.persistOK = false
			return nil
		}
		u.outbox, u.outboxAt, u.written = file, path, 0
		if u.segEntryIndex == nil {
			u.segEntryIndex = make(map[string]uint64)
		}
		u.segEntryIndex[name] = u.totalEnqueued
	}
	line, err := json.Marshal(entry)
	if err != nil {
		return nil
	}
	line = append(line, '\n')
	written, writeErr := u.outbox.Write(line)
	if writeErr != nil {
		u.persistOK = false
		return nil
	}
	u.written += int64(written)
	return u.enforceDiskBudgetLocked(instDir)
}

// enforceDiskBudgetLocked 总磁盘预算超限时 drop-oldest 段并生成 gap 标记事件。
// 活动段（正在写）绝不触碰。被丢段的数据条目同步从内存队列剔除（按全局索引定位，
// 跳过 gap 标记）——丢弃即真丢，gap 区间与实际丢失逐条对齐；而 gap 标记本身一律保留
// （它们描述的是更早段的丢失，是平台知洞的唯一证据，落独立文件不占预算）。
func (u *logUploader) enforceDiskBudgetLocked(instDir string) []LogEntry {
	if u.maxBytes <= 0 {
		return nil
	}
	segments := listOutboxSegments(instDir)
	total := int64(0)
	sizes := make(map[string]int64, len(segments))
	for _, name := range segments {
		if info, err := os.Stat(filepath.Join(instDir, name)); err == nil {
			sizes[name] = info.Size()
			total += info.Size()
		}
	}
	if total <= u.maxBytes {
		return nil
	}
	cursor := loadOutboxCursor(instDir)
	var gaps []LogEntry
	for _, name := range segments {
		if total <= u.maxBytes {
			break
		}
		if filepath.Join(instDir, name) == u.outboxAt {
			break
		}
		// 游标段只丢偏移之后的未消费部分；更旧的段已消费完，丢弃不产生丢失。
		offset := int64(0)
		if name == cursor.Segment {
			offset = cursor.Offset
		}
		count, seqs := segmentSeqsFrom(filepath.Join(instDir, name), offset)
		if err := os.Remove(filepath.Join(instDir, name)); err != nil {
			break
		}
		total -= sizes[name]
		// 段首条的全局索引：游标段用"已消费数 - 段内已消费条数"，其余段用建段时快照。
		var startIndex uint64
		if name == cursor.Segment {
			consumedInSeg := countEntriesBefore(filepath.Join(instDir, name), offset)
			if u.consumedEntries >= uint64(consumedInSeg) {
				startIndex = u.consumedEntries - uint64(consumedInSeg)
			}
		} else if idx, ok := u.segEntryIndex[name]; ok {
			startIndex = idx
		}
		delete(u.segEntryIndex, name)
		if count > 0 && (name == cursor.Segment || cursor.Segment == "") {
			// 内存同步剔除：只移除全局索引落在被丢段内的数据条目，gap 标记保留。
			endIndex := startIndex + uint64(count)
			dataIdx := u.consumedEntries
			kept := u.queue[:0]
			for _, entry := range u.queue {
				if entry.Event == "log_upload.gap" {
					kept = append(kept, entry)
					continue
				}
				if dataIdx < startIndex || dataIdx >= endIndex {
					kept = append(kept, entry)
				}
				dataIdx++
			}
			u.queue = kept
		}
		if count > 0 {
			minSeq, maxSeq := seqs[0], seqs[0]
			for _, seq := range seqs {
				if seq < minSeq {
					minSeq = seq
				}
				if seq > maxSeq {
					maxSeq = seq
				}
			}
			u.dropped += uint64(count)
			gap := LogEntry{
				Type:                  "bridge_log",
				Level:                 "WARN",
				Service:               "terminal_bridge",
				Event:                 "log_upload.gap",
				Message:               "outbox 磁盘预算超限，丢弃最旧分段；丢弃区间以本事件为准",
				Timestamp:             time.Now().UTC().Format(time.RFC3339Nano),
				EventID:               deterministicUUID("gap:" + u.instance + ":" + name),
				BridgeInstanceID:      u.instance,
				ServiceName:           "terminal-bridge",
				ServiceVersion:        serviceVersion(),
				DeploymentEnvironment: bruntime.EnvOrDefault("HCI_DEPLOYMENT_ENVIRONMENT", "local"),
				Extra: map[string]any{
					"dropped_entries": count, "seq_min": minSeq, "seq_max": maxSeq, "segment": name,
				},
			}
			// gap 写独立文件（不占预算、不会被丢、不占游标字节），内存由调用方入队。
			if u.gapsFile == nil {
				if file, openErr := os.OpenFile(filepath.Join(instDir, "gaps.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); openErr == nil {
					u.gapsFile = file
				}
			}
			if u.gapsFile != nil {
				if line, err := json.Marshal(gap); err == nil {
					if _, writeErr := u.gapsFile.Write(append(line, '\n')); writeErr == nil {
						gaps = append(gaps, gap)
					}
				}
			}
			Blog("WARN", "log_upload.gap", "outbox 磁盘预算超限，已丢弃最旧分段并上报缺口", "", "", "", "", map[string]any{
				"segment": name, "dropped_entries": count, "seq_min": minSeq, "seq_max": maxSeq,
			})
		}
		if name == cursor.Segment || cursor.Segment == "" {
			cursor = outboxCursor{Segment: nextSegment(instDir, name)}
		}
	}
	saveOutboxCursor(instDir, cursor)
	return gaps
}

// segmentSeqsFrom 返回段内 offset 之后的数据条目数与 seq 列表（按文件顺序）。
// gap 标记不计入：它们落在独立文件，不占分段字节。
func segmentSeqsFrom(path string, offset int64) (int, []uint64) {
	file, err := os.Open(path)
	if err != nil {
		return 0, nil
	}
	defer file.Close()
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return 0, nil
	}
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	count := 0
	seqs := make([]uint64, 0, 64)
	for scanner.Scan() {
		var entry LogEntry
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			continue
		}
		if entry.Event == "log_upload.gap" {
			continue
		}
		count++
		seqs = append(seqs, entry.Seq)
	}
	return count, seqs
}

// countEntriesBefore 统计段内 offset 之前的数据条目数（游标段丢失判定的索引基准）。
func countEntriesBefore(path string, offset int64) int {
	file, err := os.Open(path)
	if err != nil {
		return 0
	}
	defer file.Close()
	scanner := bufio.NewScanner(io.LimitReader(file, offset))
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	count := 0
	for scanner.Scan() {
		var entry LogEntry
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			continue
		}
		if entry.Event != "log_upload.gap" {
			count++
		}
	}
	return count
}

// advanceCursorLocked 批次确认成功后把游标前进 n 字节（跨段进位），并删除已消费完的旧段。
// 调用方持有 u.Mu。只在 persistOK 时调用：落盘链路坏了磁盘与内存不再对应，宁可重传。
func (u *logUploader) advanceCursorLocked(n int64) {
	dir := outboxDir()
	instDir := outboxInstanceDir(dir, u.instance)
	if instDir == "" {
		return
	}
	cursor := loadOutboxCursor(instDir)
	segments := listOutboxSegments(instDir)
	if cursor.Segment == "" {
		if len(segments) == 0 {
			return
		}
		cursor.Segment = segments[0]
	}
	cursor.Offset += n
	for {
		info, err := os.Stat(filepath.Join(instDir, cursor.Segment))
		if err != nil {
			// 段文件意外消失：退到下一段从头消费（重复由后端幂等）。
			next := nextSegment(instDir, cursor.Segment)
			if next == "" {
				cursor = outboxCursor{}
				break
			}
			cursor = outboxCursor{Segment: next}
			continue
		}
		if cursor.Offset < info.Size() {
			break
		}
		if filepath.Join(instDir, cursor.Segment) == u.outboxAt {
			// 活动段还在写：消费到当前末尾即止，不能越过未写入的字节。
			cursor.Offset = info.Size()
			break
		}
		carry := cursor.Offset - info.Size()
		_ = os.Remove(filepath.Join(instDir, cursor.Segment))
		next := nextSegment(instDir, cursor.Segment)
		if next == "" {
			cursor = outboxCursor{}
			break
		}
		cursor = outboxCursor{Segment: next, Offset: carry}
	}
	saveOutboxCursor(instDir, cursor)
}

// nextSegment 返回字典序（时间序）上紧跟 given 之后的段名。
func nextSegment(dir, given string) string {
	for _, name := range listOutboxSegments(dir) {
		if name > given {
			return name
		}
	}
	return ""
}

// refillFromDiskLocked 内存实时队列空时从游标读回最多 memoryQueueCap 条（L2 溢出消费）。
// 游标本身不前进——只有批次确认成功后才前进，refill 只是把磁盘数据搬进内存。
func (u *logUploader) refillFromDiskLocked() {
	dir := outboxDir()
	instDir := outboxInstanceDir(dir, u.instance)
	if instDir == "" {
		return
	}
	cursor := loadOutboxCursor(instDir)
	if cursor.Segment == "" {
		segments := listOutboxSegments(instDir)
		if len(segments) == 0 {
			return
		}
		cursor.Segment = segments[0]
	}
	for len(u.queue) < memoryQueueCap {
		file, err := os.Open(filepath.Join(instDir, cursor.Segment))
		if err != nil {
			next := nextSegment(instDir, cursor.Segment)
			if next == "" {
				return
			}
			cursor = outboxCursor{Segment: next}
			continue
		}
		if _, err := file.Seek(cursor.Offset, io.SeekStart); err != nil {
			file.Close()
			return
		}
		reader := bufio.NewReaderSize(file, 64*1024)
		advanced := false
		for len(u.queue) < memoryQueueCap {
			line, readErr := reader.ReadBytes('\n')
			if readErr != nil {
				break
			}
			cursor.Offset += int64(len(line))
			var entry LogEntry
			if err := json.Unmarshal(line, &entry); err != nil {
				continue
			}
			u.queue = append(u.queue, entry)
			if entry.Seq > u.maxQueuedSeq {
				u.maxQueuedSeq = entry.Seq
			}
			advanced = true
		}
		file.Close()
		if !advanced {
			// 本段读尽：活动段等新写入，旧段滚到下一段。
			if filepath.Join(instDir, cursor.Segment) == u.outboxAt {
				return
			}
			info, err := os.Stat(filepath.Join(instDir, cursor.Segment))
			if err == nil && cursor.Offset < info.Size() {
				// 尾部不完整行（崩溃时写了一半）：跳过残段，从下一段继续。
				cursor.Offset = info.Size()
			}
			next := nextSegment(instDir, cursor.Segment)
			if next == "" {
				return
			}
			cursor = outboxCursor{Segment: next}
		}
	}
}

// recoverOutbox 进程重启后从磁盘游标恢复未发送日志（不再整文件读回清空）。
// 段文件保留到被游标消费完才删除：恢复后进程再挂，数据仍在磁盘上。
func (u *logUploader) recoverOutbox() {
	dir := outboxDir()
	if dir == "" {
		return
	}
	u.Mu.Lock()
	instance := u.instance
	u.Mu.Unlock()
	if instance == "" {
		return
	}
	migrateLegacyOutbox(dir, instance)
	u.Mu.Lock()
	u.rebuildSegEntryIndexLocked(outboxInstanceDir(dir, instance))
	before := len(u.queue)
	u.refillFromDiskLocked()
	recovered := len(u.queue) - before
	u.Mu.Unlock()
	if recovered > 0 {
		Blog("INFO", "log_upload.recovered", "已从 outbox 恢复未发送日志", "", "", "", "", map[string]any{
			"recovered": recovered, "instance": instance,
		})
	}
}

// rebuildSegEntryIndexLocked 重启后重建段首索引：以游标为原点（consumedEntries 归零），
// 游标段未消费部分记 0，后续段按条数累加；游标之前的段已消费完，无需索引。
func (u *logUploader) rebuildSegEntryIndexLocked(instDir string) {
	if instDir == "" {
		return
	}
	if u.segEntryIndex == nil {
		u.segEntryIndex = make(map[string]uint64)
	}
	cursor := loadOutboxCursor(instDir)
	segments := listOutboxSegments(instDir)
	index := uint64(0)
	started := cursor.Segment == ""
	for _, name := range segments {
		if !started {
			if name == cursor.Segment {
				started = true
			} else {
				continue
			}
		}
		offset := int64(0)
		if name == cursor.Segment {
			offset = cursor.Offset
		}
		count, _ := segmentSeqsFrom(filepath.Join(instDir, name), offset)
		u.segEntryIndex[name] = index
		index += uint64(count)
	}
}
