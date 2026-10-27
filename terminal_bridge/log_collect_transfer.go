package main

// 日志收集制品交付（P3 段① + 段②）。
//
// 第一性原理：
//  1. 20 GiB 的包不能经 WebSocket 传（JSON 文本通道），必须由 bridge 走 HTTP 直传平台；
//  2. 传输必须可断点续传：§① 用 `tail -c +N` 从 HCI 续读（HCI 实测有 GNU tail），
//     §② 用平台返回的 uploaded_parts 跳过已传分片；
//  3. 平台不做自动清理：本流程只写客户机临时文件，完成后按参数决定是否删除本地副本，
//     绝不动 HCI 上的源包（由用户按告警提示自行清理）；
//  4. 分片与整包均计算 SHA-256，供平台側 complete 校验，避免弱网静默损坏。

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"golang.org/x/net/websocket"
)

const (
	// 20 GiB @100 Mbps ≈ 27 分钟；留足余量覆盖弱网与重试。
	logCollectTransferTimeout = 2 * time.Hour
	logCollectPullBlock       = 4 * 1024 * 1024
	logCollectProgressEvery   = 64 * 1024 * 1024
	logCollectPartRetries     = 3
	logCollectPartTimeout     = 5 * time.Minute
	logCollectMaxChunkSize    = 64 * 1024 * 1024
	logCollectMinChunkSize    = 1 * 1024 * 1024
)

// 插件产物固定落在该目录；HCI ≥6.12 的 progress 会把前缀改写为 /support，需还原。
var logCollectArchivePathPattern = regexp.MustCompile(`^/sf/data/local/acli/tmp/data/log_collect/[A-Za-z0-9_.-]+\.zip$`)

// 平台数据面默认路径，与 case-service 路由保持一致。
const logCollectDirectUploadPrefix = "/api/direct/log-collect-uploads"

type logCollectTransferProgress struct {
	Stage         string `json:"stage"` // pulling | uploading | done
	Percent       int    `json:"percent"`
	PulledBytes   int64  `json:"pulled_bytes"`
	TotalSize     int64  `json:"total_size"`
	UploadedParts int    `json:"uploaded_parts"`
	PartCount     int    `json:"part_count"`
	LocalPath     string `json:"local_path,omitempty"`
	Size          int64  `json:"size,omitempty"`
	Sha256        string `json:"sha256,omitempty"`
}

// normalizeArchivePath 校验并归一化 HCI 上的压缩包路径。
//
// 只接受插件约定的目录，且不允许任何 shell 元字符——该路径最终会进入 shell 命令
// （`tail -c +N <path>`），虽然经过 shellQuote，仍先做白名单收敛。
func normalizeArchivePath(raw string) (string, error) {
	path := strings.TrimSpace(raw)
	if path == "" {
		return "", fmt.Errorf("source_path 不能为空")
	}
	// HCI ≥6.12：progress 展示的 /support/... 实际对应 /sf/data/local/acli/...
	if strings.HasPrefix(path, "/support/") {
		path = "/sf/data/local/acli/" + strings.TrimPrefix(path, "/support/")
	}
	if !logCollectArchivePathPattern.MatchString(path) {
		return "", fmt.Errorf("source_path 必须是 %s 下的 .zip 文件", "/sf/data/local/acli/tmp/data/log_collect/")
	}
	return path, nil
}

// validatePlatformURL 只接受 http/https 且必须带 host，避免 file:// 等协议被用于读本地。
func validatePlatformURL(raw string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(raw), "/")
	if trimmed == "" {
		return "", fmt.Errorf("platform_url 不能为空")
	}
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return "", fmt.Errorf("platform_url 解析失败: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("platform_url 仅支持 http/https")
	}
	if parsed.Host == "" {
		return "", fmt.Errorf("platform_url 缺少主机名")
	}
	return trimmed, nil
}

func validUploadedParts(raw []int, partCount int) map[int]bool {
	uploaded := make(map[int]bool, len(raw))
	for _, n := range raw {
		if n >= 1 && n <= partCount {
			uploaded[n] = true
		}
	}
	return uploaded
}

// buildPullCommand 构造从节点顺序读取的远端命令。
//
// 为什么是 `tail -c +N` 而不是 SFTP/range：HCI 侧实测只有 GNU tail 可用，
// 且没有可依赖的 range 支持；`tail -c +N` 的 N 是 **1-based** 起始字节，
// 所以本地已有 offset 字节时必须传 offset+1，差一会导致重传首字节或漏一字节，
// 两者都会让最终的包摘要对不上，却要等整个包传完才暴露。
//
// 路径仍走 shellQuote：命令进的是远端 shell，路径来自平台下发的 archive_path，
// 不因为"平台可信"就放弃转义。
func buildPullCommand(remotePath string, offset int64) string {
	return fmt.Sprintf("tail -c +%d %s", offset+1, shellQuote(remotePath))
}

// pullStart 描述一次下载的起点决策。
type pullStart struct {
	// Offset 是本次从节点读取的起始字节位置（本地已有字节数）。
	Offset int64
	// Discard 为真表示本地残包不可信，必须先删除再从头下载。
	Discard bool
	// Complete 为真表示本地已持有完整包，无需任何网络读取。
	Complete bool
}

// resolvePullStart 依据两个物理事实决定续传起点：本地文件大小、远端源包大小。
//
// 三种边界各自的含义：
//   - 本地为空或小于源包：正常续传，从已有字节处接着读；
//   - 本地恰好等于源包：已传完，直接收工（重跑交付时能省掉整包重传）；
//   - 本地大于源包：**必定**不是本次的包——同一份数据不可能是自己的前缀，
//     只能是被同名覆盖/截断过的旧产物，追加进去会得到永远校验不过的坏包。
//
// remoteSize <= 0 表示远端大小未知（stat 失败）：此时无法判定完成，
// 也不该丢弃本地数据（可能是正常的半包），一律按续传处理。
func resolvePullStart(localSize, remoteSize int64) pullStart {
	if remoteSize <= 0 {
		return pullStart{Offset: localSize}
	}
	if localSize > remoteSize {
		return pullStart{Offset: 0, Discard: true}
	}
	if localSize == remoteSize {
		return pullStart{Offset: localSize, Complete: true}
	}
	return pullStart{Offset: localSize}
}

// pullArchive 把 HCI 上的压缩包续传到本地。
//
// 断点依据是「本地文件已有大小」：HCI 侧用 `tail -c +offset+1` 从断点继续读，
// 因此不需要 SFTP，也不依赖服务器端支持 range。
func (s *SSHSession) pullArchive(
	ctx context.Context,
	remotePath string,
	localPath string,
	totalSize int64,
	onProgress func(written, total int64),
) (int64, error) {
	if err := os.MkdirAll(filepath.Dir(localPath), 0o700); err != nil {
		return 0, err
	}

	var localSize int64
	if info, err := os.Stat(localPath); err == nil {
		localSize = info.Size()
	}
	start := resolvePullStart(localSize, totalSize)
	if start.Discard {
		os.Remove(localPath)
	}
	if start.Complete {
		onProgress(start.Offset, totalSize)
		return start.Offset, nil
	}
	offset := start.Offset

	session, err := s.client.NewSession()
	if err != nil {
		return offset, fmt.Errorf("创建下载会话失败: %w", err)
	}
	defer session.Close()

	stdout, err := session.StdoutPipe()
	if err != nil {
		return offset, fmt.Errorf("获取下载管道失败: %w", err)
	}
	// tail -c +N 的 N 为 1-based 起始字节位置。
	command := buildPullCommand(remotePath, offset)
	if err := session.Start(command); err != nil {
		return offset, fmt.Errorf("启动下载失败: %w", err)
	}

	file, err := os.OpenFile(localPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return offset, fmt.Errorf("打开本地文件失败: %w", err)
	}
	defer file.Close()

	written := offset
	lastReport := offset
	buffer := make([]byte, logCollectPullBlock)

	for {
		if ctx.Err() != nil {
			_ = session.Close()
			return written, ctx.Err()
		}
		n, readErr := stdout.Read(buffer)
		if n > 0 {
			if _, err := file.Write(buffer[:n]); err != nil {
				_ = session.Close()
				return written, fmt.Errorf("写入本地文件失败: %w", err)
			}
			written += int64(n)
			if written-lastReport >= logCollectProgressEvery {
				lastReport = written
				onProgress(written, totalSize)
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = session.Close()
			return written, fmt.Errorf("拉取中断: %w", readErr)
		}
	}

	if err := session.Wait(); err != nil {
		return written, fmt.Errorf("拉取命令失败（源包可能已被清理）: %w", err)
	}
	onProgress(written, totalSize)
	return written, nil
}

// fileSHA256 计算整包校验和；20 GiB 约需数十秒磁盘读。
func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()

	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

// putPart 上传单个分片。4xx 视为不可重试的客户端错误，由调用方决定是否终止。
func putPart(ctx context.Context, client *http.Client, target, token, sha256Hex string, payload []byte) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, target, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	request.Header.Set("X-Upload-Token", token)
	request.Header.Set("X-Part-SHA256", sha256Hex)
	request.Header.Set("Content-Type", "application/octet-stream")
	request.ContentLength = int64(len(payload))

	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)

	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("平台返回 HTTP %d", response.StatusCode)
	}
	return nil
}

// uploadArchiveParts 顺序上传缺失分片，跳过 uploaded_parts 中已完成的序号。
func uploadArchiveParts(
	ctx context.Context,
	platformURL string,
	taskID string,
	token string,
	localPath string,
	chunkSize int64,
	partCount int,
	totalSize int64,
	uploaded map[int]bool,
	onProgress func(uploadedParts int),
) error {
	file, err := os.Open(localPath)
	if err != nil {
		return fmt.Errorf("打开本地文件失败: %w", err)
	}
	defer file.Close()

	client := &http.Client{Timeout: logCollectPartTimeout}
	done := len(uploaded)
	target := fmt.Sprintf("%s%s/%s/parts", platformURL, logCollectDirectUploadPrefix, taskID)

	for number := 1; number <= partCount; number++ {
		if uploaded[number] {
			continue
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}

		offset := int64(number-1) * chunkSize
		expected := chunkSize
		if totalSize > 0 && offset+expected > totalSize {
			expected = totalSize - offset
		}
		if expected <= 0 {
			return fmt.Errorf("分片 %d 大小计算异常", number)
		}

		payload := make([]byte, expected)
		if _, err := file.ReadAt(payload, offset); err != nil && err != io.EOF {
			return fmt.Errorf("读取分片 %d 失败: %w", number, err)
		}
		digest := sha256.Sum256(payload)

		var lastErr error
		for attempt := 1; attempt <= logCollectPartRetries; attempt++ {
			lastErr = putPart(ctx, client, fmt.Sprintf("%s/%d", target, number), token,
				hex.EncodeToString(digest[:]), payload)
			if lastErr == nil {
				break
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
		if lastErr != nil {
			return fmt.Errorf("分片 %d 上传失败: %w", number, lastErr)
		}
		done++
		onProgress(done)
	}
	return nil
}

// handleLogCollectTransfer 执行「HCI 拉取 → 平台分块上传」。
//
// 平台的 init / complete 由前端调用（需要客户身份），bridge 只拿到数据面 token，
// 因此这里不接触控制面接口，职责边界清晰。
func (b *Bridge) handleLogCollectTransfer(
	ctx context.Context,
	ws *websocket.Conn,
	s *SSHSession,
	msg InMessage,
	cui string,
	traceID string,
) error {
	taskID := strings.TrimSpace(msg.ExecID)
	emit := func(status string, progress logCollectTransferProgress, message, errorCode string) {
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: "transfer",
			Status: status, LogData: progress, Message: message,
			ErrorCode: errorCode, TraceID: traceID, CustomUI: cui,
		})
	}

	fail := func(code, message string, progress logCollectTransferProgress) error {
		progress.Stage = "failed"
		emit("error", progress, message, code)
		blog("ERROR", "log_collect.transfer_failed", "日志包交付失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "error_code": code, "error": message,
			"stage": progress.Stage, "pulled_bytes": progress.PulledBytes,
			"uploaded_parts": progress.UploadedParts, "part_count": progress.PartCount,
		})
		return fmt.Errorf("%s", message)
	}

	sourcePath, err := normalizeArchivePath(msg.TransferSourcePath)
	if err != nil {
		return fail(ErrTransferArchiveMissing, err.Error(), logCollectTransferProgress{})
	}
	platformURL, err := validatePlatformURL(msg.TransferPlatformURL)
	if err != nil {
		return fail(ErrTransferChunkFailed, err.Error(), logCollectTransferProgress{})
	}
	token := strings.TrimSpace(msg.TransferToken)
	if token == "" {
		return fail(ErrTransferChunkFailed, "upload_token 不能为空", logCollectTransferProgress{})
	}
	if taskID == "" {
		return fail(ErrTransferChunkFailed, "exec_id（平台 task_id）不能为空", logCollectTransferProgress{})
	}

	chunkSize := msg.TransferChunkSize
	if chunkSize < logCollectMinChunkSize || chunkSize > logCollectMaxChunkSize {
		return fail(ErrTransferChunkFailed, "chunk_size 超出允许范围", logCollectTransferProgress{})
	}
	if msg.TransferTotalSize > 0 && msg.TransferTotalSize > 25*1024*1024*1024 {
		return fail(ErrTransferTooLarge, "日志包超过平台上限（25 GiB）", logCollectTransferProgress{})
	}
	partCount := msg.TransferPartCount
	if partCount <= 0 && msg.TransferTotalSize > 0 {
		partCount = int((msg.TransferTotalSize + chunkSize - 1) / chunkSize)
	}
	if partCount <= 0 {
		return fail(ErrTransferChunkFailed, "part_count 无法确定", logCollectTransferProgress{})
	}

	localPath := strings.TrimSpace(msg.TransferLocalPath)
	if localPath == "" {
		localPath = filepath.Join(os.TempDir(), "hci-log-collect", taskID+".zip")
	}

	progress := logCollectTransferProgress{
		Stage: "pulling", PartCount: partCount, TotalSize: msg.TransferTotalSize,
		LocalPath: localPath,
	}
	emit("ok", progress, "", "")

	written, err := s.pullArchive(ctx, sourcePath, localPath, msg.TransferTotalSize, func(done, total int64) {
		progress.PulledBytes = done
		progress.TotalSize = total
		if total > 0 {
			progress.Percent = int(done * 50 / total)
		}
		emit("ok", progress, "", "")
	})
	progress.PulledBytes = written
	if err != nil {
		return fail(ErrTransferChunkFailed, err.Error(), progress)
	}

	info, err := os.Stat(localPath)
	if err != nil {
		return fail(ErrTransferArchiveMissing, "本地副本不存在: "+err.Error(), progress)
	}
	if msg.TransferTotalSize > 0 && info.Size() != msg.TransferTotalSize {
		return fail(ErrTransferChecksum, fmt.Sprintf("拉取后大小不一致：期望 %d，实际 %d", msg.TransferTotalSize, info.Size()), progress)
	}
	progress.Size = info.Size()

	sum, err := fileSHA256(localPath)
	if err != nil {
		return fail(ErrTransferChecksum, "计算校验和失败: "+err.Error(), progress)
	}
	progress.Sha256 = sum

	progress.Stage = "uploading"
	uploaded := validUploadedParts(msg.TransferUploadedParts, partCount)
	progress.UploadedParts = len(uploaded)
	emit("ok", progress, "", "")

	err = uploadArchiveParts(ctx, platformURL, taskID, token, localPath, chunkSize, partCount,
		progress.Size, uploaded, func(done int) {
			progress.UploadedParts = done
			progress.Percent = 50 + int(done*50/partCount)
			emit("ok", progress, "", "")
		})
	if err != nil {
		return fail(ErrTransferChunkFailed, err.Error(), progress)
	}

	progress.Stage = "done"
	progress.Percent = 100
	emit("ok", progress, "", "")
	blog("INFO", "log_collect.transfer_done", "日志包交付完成", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"task_id": taskID, "size": progress.Size, "sha256": progress.Sha256,
		"part_count": partCount, "local_path": localPath,
	})
	return nil
}
