package main

// 日志收集证据上报：bridge 只上报观测事实，结论由平台推导。
//
// 第一性原理：
//   - 「包有多大」不等于「包里有日志」：只读 zip 中央目录，绝不用 unzip -t 校验
//     （客户包全条目加密，密码不对会把一个完好的包判成损坏）；
//   - 「下发前网络好不好」要按目标分别量：P1 关心节点 → 5888，P2 关心客户机 → 平台；
//   - 「速率/ETA」是时间序列而不是瞬时值，且没有样本时必须让界面显示「—」；
//   - 所有上报都是尽力而为：证据链路失败不得影响采集与交付主流程。

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	evidenceDirectPrefix       = "/api/direct/log-collect"
	evidenceMetricInterval     = 2 * time.Second
	evidenceStallSeconds       = 60
	evidenceBandwidthBytes     = 1024 * 1024
	evidenceProbeSamples       = 5
	processLogDir              = "/sf/log/today/acli/plugins/log_collect/"
	maxProcessLogFileBytes     = 2 * 1024 * 1024
	maxProcessLogTaskBytes     = 20 * 1024 * 1024
	maxProcessLogFiles         = 5
	maxManifestEntries         = 500
	minArchiveBytesForEvidence = 1024 * 1024
)

// evidenceConfig 是一次采集任务的证据上报凭据与路径。只在内存里保存，
// 不落盘：票据绑定 task_id，进程重启后由下一次 create 重新下发。
type evidenceConfig struct {
	TaskID       string
	CaseID       string
	PlatformURL  string
	Ticket       string
	TraceID      string
	TLSInsecure  bool
	LogServerURL string
	UploadMode   string
	NodeIP       string
	CustomUI     string
	Host         string
}

var evidenceConfigs sync.Map    // task_id -> evidenceConfig
var evidenceOnce sync.Map       // 去重键 -> struct{}（包探测/进程日志只做一次）
var evidenceTLSRelaxed sync.Map // task_id -> struct{}（命中自签证书后仅本任务放宽）

// evidenceTLSInsecure 只放宽「本次任务的证据通道」。
//
// 客户内网平台普遍自签，客户机系统根证书库不可能预置该 CA；而 P1 场景前端
// 从不下发 tls_insecure（授权只挂在交付 job 上），严格校验会让 4443 四张证据表
// 永远 0 行（Q2026111300155）。安全边界：只跳过证书校验，X-Evidence-Ticket
// 仍绑定 task_id；状态按 task_id 存内存，不落盘、不污染其它任务或交付通道。
func evidenceTLSInsecure(cfg evidenceConfig) bool {
	if cfg.TLSInsecure {
		return true
	}
	_, relaxed := evidenceTLSRelaxed.Load(cfg.TaskID)
	return relaxed
}

func markEvidenceTLSRelaxed(cfg evidenceConfig, cause error) {
	if strings.TrimSpace(cfg.TaskID) == "" {
		return
	}
	if _, loaded := evidenceTLSRelaxed.LoadOrStore(cfg.TaskID, struct{}{}); loaded {
		return
	}
	blog("WARN", "log_collect.evidence_tls_relaxed", "证据通道命中自签证书，已放宽证书校验", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
		"task_id": cfg.TaskID, "trace_id": cfg.TraceID, "reason": cause.Error(),
		"scope": "仅本次任务的证据通道", "auth": "X-Evidence-Ticket 鉴权不变",
	})
}

func configureEvidence(msg InMessage, customUI string) {
	if strings.TrimSpace(msg.ExecID) == "" || strings.TrimSpace(msg.EvidenceTicket) == "" {
		return
	}
	base, err := validatePlatformURL(msg.TransferPlatformURL)
	if err != nil {
		blog("WARN", "log_collect.evidence_disabled", "证据上报未启用：平台地址无效", msg.TraceID, msg.CaseID, msg.NodeIP, "", map[string]any{
			"reason": err.Error(), "task_id": msg.ExecID,
		})
		return
	}
	mode := "bridge_relay"
	if strings.TrimSpace(msg.RemoteURL) != "" {
		mode = "acli_direct"
	}
	cfg := evidenceConfig{
		TaskID: msg.ExecID, CaseID: msg.CaseID, PlatformURL: base,
		Ticket: msg.EvidenceTicket, TraceID: msg.TraceID, TLSInsecure: msg.TransferTLSInsecure,
		LogServerURL: msg.RemoteURL, UploadMode: mode, NodeIP: msg.NodeIP, CustomUI: customUI,
	}
	// 每次新下发都重新严格校验一次：平台换成正式 CA 后不应继续沿用旧的放宽状态。
	evidenceTLSRelaxed.Delete(cfg.TaskID)
	evidenceConfigs.Store(cfg.TaskID, cfg)
	// 下发前探测是异步的：绝不因为弱网/平台不可达拖延 acli 下发。
	go runNetworkProbes(cfg)
	blog("INFO", "log_collect.evidence_enabled", "证据上报已启用", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
		"task_id": cfg.TaskID, "upload_mode": cfg.UploadMode, "trace_id": cfg.TraceID,
	})
}

func evidenceFor(taskID string) (evidenceConfig, bool) {
	value, ok := evidenceConfigs.Load(strings.TrimSpace(taskID))
	if !ok {
		return evidenceConfig{}, false
	}
	cfg, ok := value.(evidenceConfig)
	return cfg, ok
}

// newEvidenceRequest 构造带票据的请求；重试必须重建，首次请求已消耗 body。
func (e evidenceConfig) newEvidenceRequest(ctx context.Context, target string, body []byte, probeTicket string) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Evidence-Ticket", e.Ticket)
	req.Header.Set("X-Trace-ID", e.TraceID)
	if probeTicket != "" {
		req.Header.Set("X-Probe-Ticket", probeTicket)
	}
	return req, nil
}

// postEvidence 把事实 POST 到数据面；返回响应体供测速票据二次使用。
//
// 命中自签证书时只对本次任务放宽一次并留痕，随后所有证据端点（网络探测、
// 传输指标、包内容清单、进程日志）都复用同一状态，不必每类证据各失败一轮。
func (e evidenceConfig) postEvidence(ctx context.Context, path string, payload any, probeTicket string) (map[string]any, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	target := strings.TrimRight(e.PlatformURL, "/") + path
	insecure := evidenceTLSInsecure(e)
	req, err := e.newEvidenceRequest(ctx, target, body, probeTicket)
	if err != nil {
		return nil, err
	}
	resp, err := httpClientFor(insecure, 15*time.Second).Do(req)
	if err != nil && !insecure && isUntrustedCertificateError(err) {
		markEvidenceTLSRelaxed(e, err)
		retry, buildErr := e.newEvidenceRequest(ctx, target, body, probeTicket)
		if buildErr != nil {
			return nil, buildErr
		}
		resp, err = httpClientFor(true, 15*time.Second).Do(retry)
	}
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("证据上报 HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return out, nil
}

func (e evidenceConfig) taskPath(suffix string) string {
	return fmt.Sprintf("%s/tasks/%s/%s", evidenceDirectPrefix, url.PathEscape(e.TaskID), suffix)
}

// ── 下发前网络探测 ────────────────────────────────────────────────────────

type networkSample struct {
	Target      string  `json:"target"`
	Reachable   bool    `json:"reachable"`
	SampleCount int     `json:"sample_count"`
	LossRate    float64 `json:"loss_rate"`
	RTTMinMS    *int64  `json:"rtt_min_ms,omitempty"`
	RTTAvgMS    *int64  `json:"rtt_avg_ms,omitempty"`
	RTTMaxMS    *int64  `json:"rtt_max_ms,omitempty"`
	JitterMS    *int64  `json:"jitter_ms,omitempty"`
	HTTPCode    *int    `json:"http_code,omitempty"`
	UpBps       *int64  `json:"up_bps,omitempty"`
	DownBps     *int64  `json:"down_bps,omitempty"`
	Advice      string  `json:"advice,omitempty"`
	Samples     []int64 `json:"samples"`
}

func hostPortOf(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Host == "" {
		return ""
	}
	port := parsed.Port()
	if port == "" {
		if parsed.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return net.JoinHostPort(parsed.Hostname(), port)
}

func measureNetwork(ctx context.Context, targetURL string, insecure bool) networkSample {
	address := hostPortOf(targetURL)
	sample := networkSample{Target: targetURL, Samples: []int64{}}
	if address == "" {
		sample.Advice = "目标地址无法解析"
		return sample
	}
	var rtts []int64
	for index := 0; index < evidenceProbeSamples; index++ {
		started := time.Now()
		conn, err := (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, "tcp", address)
		if err == nil {
			_ = conn.Close()
			rtts = append(rtts, time.Since(started).Milliseconds())
		} else {
			sample.Samples = append(sample.Samples, -1)
		}
		if err == nil {
			sample.Samples = append(sample.Samples, time.Since(started).Milliseconds())
		}
	}
	sample.SampleCount = evidenceProbeSamples
	sample.LossRate = float64(evidenceProbeSamples-len(rtts)) / float64(evidenceProbeSamples)
	sample.Reachable = len(rtts) > 0
	if len(rtts) > 0 {
		var sum int64
		minV, maxV := rtts[0], rtts[0]
		for _, value := range rtts {
			sum += value
			if value < minV {
				minV = value
			}
			if value > maxV {
				maxV = value
			}
		}
		avg := sum / int64(len(rtts))
		var jitter int64
		for _, value := range rtts {
			delta := value - avg
			if delta < 0 {
				delta = -delta
			}
			jitter += delta
		}
		jitter /= int64(len(rtts))
		sample.RTTMinMS, sample.RTTAvgMS, sample.RTTMaxMS, sample.JitterMS = &minV, &avg, &maxV, &jitter
	}
	reqCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, targetURL, nil)
	if err == nil {
		client := httpClientFor(insecure, 5*time.Second)
		resp, doErr := client.Do(req)
		if doErr == nil {
			code := resp.StatusCode
			sample.HTTPCode = &code
			_ = resp.Body.Close()
		}
	}
	return sample
}

func runNetworkProbes(cfg evidenceConfig) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	targets := []struct {
		name string
		url  string
	}{{"platform_data", cfg.PlatformURL}}
	if cfg.UploadMode == "acli_direct" && strings.TrimSpace(cfg.LogServerURL) != "" {
		targets = append(targets, struct {
			name string
			url  string
		}{"log_server", cfg.LogServerURL})
	}
	for _, target := range targets {
		measured := measureNetwork(ctx, target.url, evidenceTLSInsecure(cfg))
		facts := networkSample{
			Target: target.name, Reachable: measured.Reachable, SampleCount: measured.SampleCount,
			LossRate: measured.LossRate, RTTMinMS: measured.RTTMinMS, RTTAvgMS: measured.RTTAvgMS,
			RTTMaxMS: measured.RTTMaxMS, JitterMS: measured.JitterMS, HTTPCode: measured.HTTPCode,
			Samples: measured.Samples,
		}
		ack, err := cfg.postEvidence(ctx, cfg.taskPath("network-probe"), facts, "")
		if err != nil {
			blog("WARN", "log_collect.network_probe_failed", "网络探测上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
				"target": target.name, "error": err.Error(),
			})
			continue
		}
		probeID, _ := ack["probe_id"].(string)
		ticket, _ := ack["ticket"].(string)
		if probeID == "" || ticket == "" {
			continue
		}
		upBps, downBps := measureBandwidth(ctx, cfg, probeID, ticket)
		if upBps > 0 {
			facts.UpBps = &upBps
		}
		if downBps > 0 {
			facts.DownBps = &downBps
		}
		facts.Advice = bandwidthAdvice(upBps, downBps, measured.Reachable)
		_, _ = cfg.postEvidence(ctx, cfg.taskPath("network-probe"), facts, "")
	}
}

func measureBandwidth(ctx context.Context, cfg evidenceConfig, probeID, ticket string) (int64, int64) {
	base := strings.TrimRight(cfg.PlatformURL, "/") + evidenceDirectPrefix + "/network-probe/" + url.PathEscape(probeID)
	client := httpClientFor(evidenceTLSInsecure(cfg), 15*time.Second)
	payload := bytes.Repeat([]byte{'x'}, evidenceBandwidthBytes)

	started := time.Now()
	upReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/uplink", bytes.NewReader(payload))
	var upBps int64
	if err == nil {
		upReq.Header.Set("X-Probe-Ticket", ticket)
		resp, doErr := client.Do(upReq)
		if doErr == nil {
			_ = resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				upBps = int64(float64(len(payload)) / time.Since(started).Seconds())
			}
		}
	}

	started = time.Now()
	downURL := fmt.Sprintf("%s/downlink?bytes=%d", base, evidenceBandwidthBytes)
	downReq, err := http.NewRequestWithContext(ctx, http.MethodGet, downURL, nil)
	var downBps int64
	if err == nil {
		downReq.Header.Set("X-Probe-Ticket", ticket)
		resp, doErr := client.Do(downReq)
		if doErr == nil {
			received, _ := io.Copy(io.Discard, io.LimitReader(resp.Body, evidenceBandwidthBytes+1))
			_ = resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				downBps = int64(float64(received) / time.Since(started).Seconds())
			}
		}
	}
	return upBps, downBps
}

func bandwidthAdvice(upBps, downBps int64, reachable bool) string {
	if !reachable {
		return "目标不可达：请检查网络连通性或代理配置后再下发采集"
	}
	if upBps > 0 && upBps < 1024*1024 {
		return "上行带宽偏低：大包交付可能耗时较久，建议避开业务高峰或改用另一条交付路径"
	}
	if downBps > 0 && downBps < 1024*1024 {
		return "下行带宽偏低：平台侧拉取可能变慢，建议稍后重试"
	}
	return ""
}

// ── 传输速率时间序列 ──────────────────────────────────────────────────────

type transferMetric struct {
	Seq        int64    `json:"seq"`
	Stage      string   `json:"stage"`
	BytesDone  int64    `json:"bytes_done"`
	BytesTotal *int64   `json:"bytes_total,omitempty"`
	Percent    *float64 `json:"percent,omitempty"`
	ElapsedMS  int64    `json:"elapsed_ms"`
	RateBps    *int64   `json:"rate_bps,omitempty"`
	EtaSec     *int64   `json:"eta_sec,omitempty"`
	Stalled    bool     `json:"stalled"`
	Source     string   `json:"source"`
}

// streamEvidenceMetrics 每 2 秒采样一次作业进度，速率用 EMA 平滑、ETA 用剩余字节估算。
// 没有增量不补零：stalled 由平台按 60 秒阈值判定并写进 conditions。
func (r *deliveryRunner) streamEvidenceMetrics(ctx context.Context, job *deliveryJob) {
	cfg, ok := evidenceFor(job.TaskID)
	if !ok {
		return
	}
	ticker := time.NewTicker(evidenceMetricInterval)
	defer ticker.Stop()
	started := time.Now()
	lastAt := started
	var lastBytes int64
	var seq int64
	var rate float64
	var stalledSince time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			r.mu.Lock()
			phase := job.Phase
			total := job.SourceSize
			done := job.PulledBytes
			uploaded := int64(len(job.UploadedParts)) * job.ChunkSize
			r.mu.Unlock()
			if phase == phaseUploading || uploaded > 0 {
				done += uploaded
			}
			if total > 0 && done > total {
				done = total
			}
			elapsed := now.Sub(lastAt).Seconds()
			delta := done - lastBytes
			instant := float64(delta) / elapsed
			if instant < 0 {
				instant = 0
			}
			// 没有新字节就必须把速率归零：EMA 会残留一个"看似还在传"的数值，
			// 让 ETA 在停摆时继续倒数，是比显示「—」更危险的假象。
			if delta <= 0 {
				rate = 0
			} else if rate == 0 {
				rate = instant
			} else {
				rate = rate*0.7 + instant*0.3
			}
			if done == lastBytes {
				if stalledSince.IsZero() {
					stalledSince = now
				}
			} else {
				stalledSince = time.Time{}
			}
			stage := "fetching"
			if phase == phaseUploading || uploaded > 0 {
				stage = "uploading"
			}
			metric := transferMetric{
				Seq: seq, Stage: stage, BytesDone: done, ElapsedMS: now.Sub(started).Milliseconds(),
				Stalled: !stalledSince.IsZero() && now.Sub(stalledSince) >= evidenceStallSeconds*time.Second,
				Source:  "bridge",
			}
			seq++
			if total > 0 {
				percent := float64(done) / float64(total) * 100
				metric.BytesTotal = &total
				metric.Percent = &percent
			}
			if rate > 0 {
				rounded := int64(rate)
				metric.RateBps = &rounded
				if total > done {
					eta := int64(float64(total-done) / rate)
					metric.EtaSec = &eta
				}
			}
			postCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
			_, err := cfg.postEvidence(postCtx, cfg.taskPath("metrics"), metric, "")
			cancel()
			if err != nil {
				blog("WARN", "log_collect.metric_failed", "传输速率样本上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
					"task_id": cfg.TaskID, "seq": metric.Seq, "error": err.Error(),
				})
			}
			lastAt, lastBytes = now, done
		}
	}
}

// ── 包内容探测 ────────────────────────────────────────────────────────────

type manifestEntry struct {
	Name       string `json:"name,omitempty"`
	Size       int64  `json:"size,omitempty"`
	Compressed int64  `json:"compressed,omitempty"`
	Encrypted  bool   `json:"encrypted,omitempty"`
}

type artifactManifest struct {
	Source          string          `json:"source"`
	ArchivePath     string          `json:"archive_path,omitempty"`
	ArchiveSize     int64           `json:"archive_size,omitempty"`
	ArchiveSHA256   string          `json:"archive_sha256,omitempty"`
	EntryCount      int             `json:"entry_count"`
	EntryTotalBytes int64           `json:"entry_total_bytes"`
	Entries         []manifestEntry `json:"entries"`
	HasEncrypted    bool            `json:"has_encrypted"`
	Readable        bool            `json:"readable"`
	Evidence        map[string]any  `json:"evidence"`
}

// inspectLocalArchive 只读本地 zip 中央目录，绝不解压内容、绝不校验收密条目。
func inspectLocalArchive(path string) artifactManifest {
	result := artifactManifest{Source: "node_zip", ArchivePath: path, Entries: []manifestEntry{}}
	info, err := os.Stat(path)
	if err == nil {
		result.ArchiveSize = info.Size()
	}
	reader, err := zip.OpenReader(path)
	if err != nil {
		result.Evidence = map[string]any{"reason": err.Error()}
		return result
	}
	defer reader.Close()
	result.Readable = true
	for _, entry := range reader.File {
		result.EntryCount++
		if len(result.Entries) < maxManifestEntries {
			result.Entries = append(result.Entries, manifestEntry{
				Name:       filepath.Base(entry.Name),
				Size:       int64(entry.UncompressedSize64),
				Compressed: int64(entry.CompressedSize64),
				Encrypted:  entry.Flags&0x1 != 0,
			})
		}
		result.EntryTotalBytes += int64(entry.UncompressedSize64)
		if entry.Flags&0x1 != 0 {
			result.HasEncrypted = true
		}
	}
	result.Evidence = map[string]any{
		"min_archive_bytes": minArchiveBytesForEvidence,
		"reader":            "archive/zip central directory",
		"entries_capped":    result.EntryCount > maxManifestEntries,
	}
	return result
}

func reportLocalArchive(ctx context.Context, cfg evidenceConfig, path string) {
	if strings.TrimSpace(path) == "" {
		return
	}
	key := "manifest:" + cfg.TaskID + ":" + path
	if _, loaded := evidenceOnce.LoadOrStore(key, struct{}{}); loaded {
		return
	}
	manifest := inspectLocalArchive(path)
	if _, err := cfg.postEvidence(ctx, cfg.taskPath("artifact-manifest"), manifest, ""); err != nil {
		blog("WARN", "log_collect.artifact_manifest_failed", "包内容探测上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "error": err.Error(),
		})
	}
}

// inspectNodeArchive 在节点侧读中央目录（P1 包不在客户机时也能拿到条目事实）。
// 只读命令：zipinfo -t / unzip -Z1，不解压、不做完整性校验。
func inspectNodeArchive(ctx context.Context, s *SSHSession, cfg evidenceConfig, path string, size int64) {
	if s == nil || strings.TrimSpace(path) == "" {
		return
	}
	key := "node-manifest:" + cfg.TaskID + ":" + path + ":" + strconv.FormatInt(size, 10)
	if _, loaded := evidenceOnce.LoadOrStore(key, struct{}{}); loaded {
		return
	}
	quoted := shellQuote(path)
	namesOut, _, namesCode, namesErr := s.runQuickCommandStreams(ctx, "unzip -Z1 "+quoted+" | head -n "+strconv.Itoa(maxManifestEntries))
	totalsOut, _, totalsCode, totalsErr := s.runQuickCommandStreams(ctx, "zipinfo -t "+quoted)
	readable := namesErr == nil && namesCode == 0 && totalsErr == nil && totalsCode == 0
	manifest := artifactManifest{
		Source: "node_zip", ArchivePath: path, ArchiveSize: size,
		Readable: readable, Entries: []manifestEntry{},
		Evidence: map[string]any{"reader": "unzip -Z1 + zipinfo -t"},
	}
	for _, name := range strings.Split(strings.TrimSpace(namesOut), "\n") {
		if strings.TrimSpace(name) == "" {
			continue
		}
		manifest.EntryCount++
		manifest.Entries = append(manifest.Entries, manifestEntry{Name: filepath.Base(name)})
	}
	// zipinfo -t 形如 "982 files, 10293010092 bytes uncompressed, ..."。
	numbers := regexp.MustCompile(`[0-9]+`).FindAllString(totalsOut, 3)
	if len(numbers) >= 2 {
		if parsed, err := strconv.ParseInt(numbers[1], 10, 64); err == nil {
			manifest.EntryTotalBytes = parsed
		}
	}
	if !readable {
		manifest.Evidence["reason"] = "节点侧中央目录读取不可用，仅保留体积证据"
	}
	if _, err := cfg.postEvidence(ctx, cfg.taskPath("artifact-manifest"), manifest, ""); err != nil {
		blog("WARN", "log_collect.node_manifest_failed", "节点侧包内容探测上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "error": err.Error(),
		})
	}
}

// ── 采集进程日志 ──────────────────────────────────────────────────────────

type processLogPayload struct {
	Host         string `json:"host"`
	Path         string `json:"path"`
	Size         int    `json:"size"`
	SHA256       string `json:"sha256,omitempty"`
	ContentGzB64 string `json:"content_gz_b64,omitempty"`
	Truncated    bool   `json:"truncated"`
	Status       string `json:"status"`
	Error        string `json:"error,omitempty"`
	CollectedAt  string `json:"collected_at,omitempty"`
}

func collectNodeProcessLogs(ctx context.Context, s *SSHSession, cfg evidenceConfig) {
	if s == nil {
		return
	}
	key := "process-logs:" + cfg.TaskID
	if _, loaded := evidenceOnce.LoadOrStore(key, struct{}{}); loaded {
		return
	}
	listOut, _, code, err := s.runQuickCommandStreams(ctx, "ls -1 "+shellQuote(processLogDir))
	if err != nil || code != 0 {
		blog("WARN", "log_collect.process_logs_unavailable", "采集进程日志目录不可读", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "error": errString(err), "exit_code": code,
		})
		return
	}
	var used int64
	count := 0
	for _, name := range strings.Split(strings.TrimSpace(listOut), "\n") {
		name = strings.TrimSpace(name)
		if name == "" || strings.Contains(name, "/") || strings.Contains(name, "..") {
			continue
		}
		if count >= maxProcessLogFiles || used >= maxProcessLogTaskBytes {
			break
		}
		path := processLogDir + name
		content, _, contentCode, contentErr := s.runQuickCommandStreams(ctx, "tail -c "+strconv.Itoa(maxProcessLogFileBytes)+" "+shellQuote(path))
		payload := processLogPayload{
			Host: cfg.NodeIP, Path: path, CollectedAt: time.Now().UTC().Format(time.RFC3339),
		}
		switch {
		case contentErr != nil || contentCode != 0:
			payload.Status = "error"
			payload.Error = firstNonEmpty(errString(contentErr), "节点侧读取失败")
		case strings.TrimSpace(content) == "":
			payload.Status = "empty"
		default:
			raw := []byte(content)
			if len(raw) > maxProcessLogFileBytes {
				raw = raw[:maxProcessLogFileBytes]
				payload.Truncated = true
			}
			var compressed bytes.Buffer
			writer := gzip.NewWriter(&compressed)
			_, _ = writer.Write(raw)
			_ = writer.Close()
			sum := sha256.Sum256(raw)
			payload.Status = "ok"
			payload.Size = len(raw)
			payload.SHA256 = hex.EncodeToString(sum[:])
			payload.ContentGzB64 = base64.StdEncoding.EncodeToString(compressed.Bytes())
			used += int64(len(raw))
		}
		postCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		_, postErr := cfg.postEvidence(postCtx, cfg.taskPath("process-logs"), payload, "")
		cancel()
		if postErr != nil {
			blog("WARN", "log_collect.process_log_failed", "采集进程日志上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
				"task_id": cfg.TaskID, "path": path, "error": postErr.Error(),
			})
		}
		count++
	}
}
