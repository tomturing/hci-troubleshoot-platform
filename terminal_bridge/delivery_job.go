package main

// 日志包交付作业（P2 中转）：可恢复、可续传、与浏览器解耦。
//
// 第一性原理
// ----------
// 一次交付只有三件事会变：**已拉多少字节**、**已传哪些分片**、**现在处于哪个阶段**。
// 只要这三件事能落盘并在重启后读回，交付就能从任意中断点继续；
// 至于"谁在驱动它"（浏览器/bridge），只是执行权问题，不该影响状态本身。
//
// 因此这里把状态从 goroutine 内存搬到磁盘，执行权交给单例 runner。
//
// 对抗性审查
// ----------
//  1. 平台任务状态与 job 文件不一致 → 恢复时先查平台任务，平台已 completed 则直接归档；
//  2. 本地临时包丢失 → 从 0 重拉，但已传分片仍按平台 uploaded_parts 跳过；
//  3. 分片令牌过期 → complete 前若 401，标记 needs_reauth 交前端续签，不静默重试；
//  4. 采集未完成就拉包 → 阶段机强制 waiting_collect，且只读 progress，绝不 cancel。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// 交付模式。由平台/前端在 delivery_submit 中显式声明，bridge 不猜。
const (
	// deliveryModeCollect 默认：对应"采集刚发起，等它写完再搬"。
	deliveryModeCollect = "collect"
	// deliveryModeDirect 补交付：源包已由上一次采集产出，跳过 progress 直接搬。
	deliveryModeDirect = "direct"
)

// deliveryModeIsDirect 判定本次交付是否为"补交付"。
//
// 只认显式声明：空值/未知值一律按 collect 处理，因为 collect 会先读一遍 progress
// 再动手，是更保守的一侧。反过来"猜成 direct"会在采集尚未写完时把半截包搬走。
// 单独抽成函数是为了让这条约定能被单测锁住——它是"已采集完成可直接上传"的入口开关。
func deliveryModeIsDirect(mode string) bool {
	return strings.EqualFold(strings.TrimSpace(mode), deliveryModeDirect)
}

// deliveryPhase 是交付阶段。顺序即推进顺序，恢复时按它决定从哪继续。
type deliveryPhase string

const (
	phaseWaitingCollect deliveryPhase = "waiting_collect" // 等 acli 采完（只读 progress）
	phasePulling        deliveryPhase = "pulling"         // HCI → bridge 本地
	phaseUploading      deliveryPhase = "uploading"       // 本地 → 平台分片
	phaseCompleting     deliveryPhase = "completing"      // 合并 + 触发代传
	phaseDone           deliveryPhase = "done"
	phaseFailed         deliveryPhase = "failed"
)

// terminalPhase 判断是否终态（终态不再自动重试）。
func (p deliveryPhase) terminal() bool {
	return p == phaseDone || p == phaseFailed
}

// deliveryJob 是一次交付的完整事实。字段刻意扁平，便于人工 cat 文件排障。
type deliveryJob struct {
	JobID       string        `json:"job_id"`
	TaskID      string        `json:"task_id"`
	CaseID      string        `json:"case_id"`
	NodeIP      string        `json:"node_ip"`
	SourcePath  string        `json:"source_path"`
	LocalPath   string        `json:"local_path"`
	PlatformURL string        `json:"platform_url"`
	UploadToken string        `json:"upload_token"`
	Phase       deliveryPhase `json:"phase"`
	// SourceSize 为 0 表示尚未 stat 成功（恢复时重新量一次）。
	SourceSize    int64  `json:"source_size"`
	PulledBytes   int64  `json:"pulled_bytes"`
	ChunkSize     int64  `json:"chunk_size"`
	PartCount     int    `json:"part_count"`
	UploadedParts []int  `json:"uploaded_parts"`
	Attempt       int    `json:"attempt"`
	LastError     string `json:"last_error"`
	// NeedsReauth 表示 SSH 凭据或上传令牌已失效，需要前端重新下发后才能继续。
	// 这是明确承认的边界：凭据不能明文落盘，进程重启后必然要重新认证。
	NeedsReauth bool `json:"needs_reauth"`
	// Direct 表示"补交付已有包"：源包已由上一次采集产出，不必也不该再等 progress。
	// 交付的充分必要条件只有两条——节点上存在完整包、平台有上传会话；
	// 采集是否仍在跑与"能不能传这个包"没有因果关系。
	Direct bool `json:"direct,omitempty"`
	// TLSInsecure 表示本次交付允许跳过平台证书校验。
	// 平台在客户内网普遍是自签证书，客户机系统根证书库不可能预置它，
	// 不放宽就必然 x509 失败。这里不做全局开关：授权随 job 落盘，
	// 只影响本任务的请求，用户撤销后新 job 自然恢复校验。
	TLSInsecure bool   `json:"tls_insecure,omitempty"`
	CreatedAt   string `json:"created_at"`
	UpdatedAt   string `json:"updated_at"`
}

// progress 返回 0-100 的整体进度：拉取占前 50%，上传占后 50%。
func (j *deliveryJob) progress() int {
	switch j.Phase {
	case phaseDone:
		return 100
	case phaseUploading, phaseCompleting:
		if j.PartCount <= 0 {
			return 50
		}
		return 50 + len(j.UploadedParts)*50/j.PartCount
	case phasePulling:
		if j.SourceSize <= 0 {
			return 0
		}
		return int(j.PulledBytes * 50 / j.SourceSize)
	default:
		return 0
	}
}

// addUploadedPart 幂等登记已传分片（并发窗口完成顺序不确定，必须去重）。
func (j *deliveryJob) addUploadedPart(part int) {
	for _, existing := range j.UploadedParts {
		if existing == part {
			return
		}
	}
	j.UploadedParts = append(j.UploadedParts, part)
	sort.Ints(j.UploadedParts)
}

// uploadedSet 把切片转成查表结构，避免每片 O(n) 扫描。
func (j *deliveryJob) uploadedSet() map[int]bool {
	set := make(map[int]bool, len(j.UploadedParts))
	for _, part := range j.UploadedParts {
		set[part] = true
	}
	return set
}

// deliveryStore 负责 job 的落盘与恢复。
//
// 写盘用 tmp+rename：客户机随时可能断电/被杀，半截 JSON 会让整个 job 不可读，
// 而"读不出来的 job"等于把数据永久卡死。
type deliveryStore struct {
	mu  sync.Mutex
	dir string
}

func newDeliveryStore(logDir string) *deliveryStore {
	if strings.TrimSpace(logDir) == "" {
		return &deliveryStore{}
	}
	return &deliveryStore{dir: filepath.Join(logDir, "delivery-jobs")}
}

// enabled 表示落盘目录可用；不可用时 job 只能跑在内存里（重启即丢，但功能不退化）。
func (s *deliveryStore) enabled() bool { return s != nil && s.dir != "" }

func (s *deliveryStore) path(jobID string) string {
	return filepath.Join(s.dir, jobID+".json")
}

// save 原子写入：先写 .tmp 再 rename，避免半截文件。
func (s *deliveryStore) save(job *deliveryJob) error {
	if !s.enabled() {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.MkdirAll(s.dir, 0o700); err != nil {
		return err
	}
	job.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	payload, err := json.MarshalIndent(job, "", "  ")
	if err != nil {
		return err
	}
	path := s.path(job.JobID)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, payload, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// load 读取单个 job；文件损坏时返回 nil（调用方据此跳过，不阻塞其它 job）。
func (s *deliveryStore) load(jobID string) *deliveryJob {
	if !s.enabled() {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, err := os.ReadFile(s.path(jobID))
	if err != nil {
		return nil
	}
	var job deliveryJob
	if err := json.Unmarshal(raw, &job); err != nil {
		return nil
	}
	return &job
}

// list 扫描目录内所有可恢复的 job（非终态）。
func (s *deliveryStore) list() []*deliveryJob {
	if !s.enabled() {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return nil
	}
	jobs := make([]*deliveryJob, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		raw, readErr := os.ReadFile(filepath.Join(s.dir, entry.Name()))
		if readErr != nil {
			continue
		}
		var job deliveryJob
		if jsonErr := json.Unmarshal(raw, &job); jsonErr != nil {
			continue
		}
		if job.Phase.terminal() {
			continue
		}
		jobs = append(jobs, &job)
	}
	return jobs
}

// purgeOlderThan 清理终态且超期的 job 文件，避免客户机磁盘被历史记录堆满。
func (s *deliveryStore) purgeOlderThan(maxAge time.Duration) int {
	if !s.enabled() {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return 0
	}
	removed := 0
	deadline := time.Now().Add(-maxAge)
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		info, infoErr := entry.Info()
		if infoErr != nil || info.ModTime().After(deadline) {
			continue
		}
		if removeErr := os.Remove(filepath.Join(s.dir, entry.Name())); removeErr == nil {
			removed++
		}
	}
	return removed
}
