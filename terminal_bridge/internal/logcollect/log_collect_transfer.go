package logcollect

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
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"golang.org/x/net/websocket"
	"hash"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/logcontract"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/shared/uplink"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"time"
)

const (
	// 20 GiB @100 Mbps ≈ 27 分钟；留足余量覆盖弱网与重试。
	logCollectTransferTimeout = 2 * time.Hour
	logCollectPullBlock       = remote.DefaultPullBlock
	// S1 收口：分片上传原语归 shared/uplink，这里保留别名使既有引用不断。
	LogCollectPartRetries  = uplink.PartRetries
	LogCollectPartTimeout  = uplink.DefaultPartTimeout
	logCollectMaxChunkSize = 64 * 1024 * 1024
	logCollectMinChunkSize = 1 * 1024 * 1024
)

// ErrPullSizeMismatch 表示"拉到的字节数与源包不符"。
//
// 必须与网络中断区分：网络断了可以续传，源包被截断/同名覆盖则重试多少次都一样。
// 用哨兵错误让调用方能判定"这是数据问题，不是链路问题"。
//
// S1 收口：哨兵本体已迁到 remote.ErrPullSizeMismatch，这里保留别名，
// 使 delivery_runner 与 handleLogCollectTransfer 里的 errors.Is 判定链不断。
var ErrPullSizeMismatch = remote.ErrPullSizeMismatch

// 平台数据面默认路径，与 case-service 路由保持一致（别名：唯一实现在 uplink）。
const LogCollectDirectUploadPrefix = uplink.DirectUploadPrefix

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

// NormalizeArchivePath 保留为域内薄包装：实现已归 remote.NormalizeArchivePath
// （交付域在拉包前也要做同一份白名单校验）。
func NormalizeArchivePath(raw string) (string, error) {
	return remote.NormalizeArchivePath(raw)
}

// ValidatePlatformURL 保留为域内薄包装：实现已归 uplink.ValidateURL（交付域同样需要）。
func ValidatePlatformURL(raw string) (string, error) {
	return uplink.ValidateURL(raw)
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
// 路径仍走 ShellQuote：命令进的是远端 shell，路径来自平台下发的 archive_path，
// 不因为"平台可信"就放弃转义。
//
// offset==0 时改用 `cat`：`tail -c +1` 与 `cat` 逐字节等价，而 aSV 受限 shell 的
// 命令白名单里确有 cat、未必有 tail（同源证据：Q2026102900165 现场 stat 就是
// exit 127，而 ls/cat 可用）。首次交付必然从 0 开始，这条替换覆盖绝大多数路径，
// 且不存在语义差异——不必等现场因为没有 tail 再挂一次。
func buildPullCommand(remotePath string, offset int64) string {
	return remote.BuildPullCommand(remotePath, offset)
}

// pullStart 描述一次下载的起点决策（别名：唯一实现在 remote/transfer.go）。
type pullStart = remote.PullStart

// resolvePullStart 依据两个物理事实决定续传起点：本地文件大小、远端源包大小。
//
// 三种边界（本地小于/等于/大于源包）与 remoteSize<=0 的降级口径，
// 论证见 remote.ResolvePullStart——尤其是"本地大于源包必定不是本次的包"：
// 同一份数据不可能是自己的前缀，追加进去会得到永远校验不过的坏包。
func resolvePullStart(localSize, remoteSize int64) pullStart {
	return remote.ResolvePullStart(localSize, remoteSize)
}

// hashFilePrefixStrict 把 path 的前 n 字节喂给 hasher。
//
// 续传时本地已有 offset 字节，必须先把它们重新摘要一遍，否则最终哈希只覆盖
// 后半段——那比不提供哈希更糟：看起来"有校验"，实际永远对不上。
// 短读即报错（与 sftp 下载侧允许 .part 短读的口径不同）。
func hashFilePrefixStrict(path string, n int64, hasher hash.Hash) error {
	return remote.HashFilePrefixStrict(path, n, hasher)
}

// progressStep 决定进度上报的字节步进（总量 1% 夹在 [4 MiB, 64 MiB]）。
//
// 固定 64 MiB 在小包上等于"全程不上报"：88 MB 的包从头到尾一次都不会触发，
// 失败时 job.PulledBytes 恒为 0，而错误文案里却写着"已拉 9.3 MB"
// （2026-11-03 Q2026110300988）。
func progressStep(total int64) int64 {
	return remote.ProgressStep(total)
}

// PullArchive 把 HCI 上的压缩包续传到本地，同时返回整包 SHA-256。
//
// S1 收口：断点续传、边拉边摘要、字节数收敛三件事的唯一实现已迁到
// remote.Pull。本方法只剩"把 sshx.SSHSession 适配成执行器 + 拆三元组返回值"。
//
// 为什么 delivery_runner.pull 不需要再改：它本来就调用本方法，外壳里的
// "每次重新 stat 量源包 / 长任务心跳抗 TMOUT=600 / 磁盘 admission 预检 /
// 失败回填 PulledBytes" 都是交付编排语义，不属拉取原语，因此原地不动。
// 收口前"两份拉取实现"的差异由此消除：两个调用方现在共用同一段字节逻辑。
func PullArchive(r remote.StreamRunner, ctx context.Context,
	remotePath string,
	localPath string,
	totalSize int64,
	onProgress func(written, total int64)) (int64, string, error) {
	result, err := remote.Pull(ctx, r, remote.PullSpec{
		RemotePath: remotePath,
		LocalPath:  localPath,
		TotalSize:  totalSize,
		BlockSize:  logCollectPullBlock,
		OnProgress: onProgress,
	})
	return result.Written, result.SHA256, err
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

// FileDigest 保留为域内薄包装：实现已归 uplink.FileDigest（交付域校验本地副本用）。
func FileDigest(path, algo string) (string, error) {
	return uplink.FileDigest(path, algo)
}

// handleLogCollectTransfer 执行「HCI 拉取 → 平台分块上传」。
//
// 平台的 init / complete 由前端调用（需要客户身份），bridge 只拿到数据面 token，
// 因此这里不接触控制面接口，职责边界清晰。
func handleLogCollectTransfer(b *session.Hub,
	ctx context.Context,
	ws *websocket.Conn,
	s *sshx.SSHSession,
	msg models.InMessage,
	cui string,
	traceID string,
) error {
	// 长任务标记：本函数跑在独立 goroutine（ws_routes.go 的 go HandleLogCollect），
	// 与发起它的浏览器连接完全解耦。页面一关 refs 立刻归零，只剩 15 分钟业务活跃窗
	// （DefaultSessionBusyGrace）兜底，而拉取预算高达 2 小时——窗一过期，reaper 就会
	// 把仍在传的会话当成空闲回收，PullArchive 的 SSH 通道被 close，整包前功尽弃。
	// 成对包裹期间 ReapCandidates 直接跳过本会话，结束即自动恢复租约判定。
	if s != nil {
		s.BeginLongTask()
		defer s.EndLongTask()
	}

	taskID := strings.TrimSpace(msg.ExecID)
	emit := func(status string, progress logCollectTransferProgress, message, errorCode string) {
		models.SendMsg(ws, models.OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: "transfer",
			Status: status, LogData: progress, Message: message,
			ErrorCode: errorCode, TraceID: traceID, CustomUI: cui,
		})
	}

	fail := func(code, message string, progress logCollectTransferProgress) error {
		progress.Stage = "failed"
		emit("error", progress, message, code)
		evidence.Emit("ERROR", "log_collect.transfer_failed", "日志包交付失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "error_code": code, "error": message,
			"stage": progress.Stage, "pulled_bytes": progress.PulledBytes,
			"uploaded_parts": progress.UploadedParts, "part_count": progress.PartCount,
		})
		return fmt.Errorf("%s", message)
	}

	sourcePath, err := NormalizeArchivePath(msg.TransferSourcePath)
	if err != nil {
		return fail(logcontract.ErrTransferArchiveMissing, err.Error(), logCollectTransferProgress{})
	}
	platformURL, err := ValidatePlatformURL(msg.PlatformURL)
	if err != nil {
		return fail(logcontract.ErrTransferChunkFailed, err.Error(), logCollectTransferProgress{})
	}
	token := strings.TrimSpace(msg.TransferToken)
	if token == "" {
		return fail(logcontract.ErrTransferChunkFailed, "upload_token 不能为空", logCollectTransferProgress{})
	}
	if taskID == "" {
		return fail(logcontract.ErrTransferChunkFailed, "exec_id（平台 task_id）不能为空", logCollectTransferProgress{})
	}

	chunkSize := msg.TransferChunkSize
	if chunkSize < logCollectMinChunkSize || chunkSize > logCollectMaxChunkSize {
		return fail(logcontract.ErrTransferChunkFailed, "chunk_size 超出允许范围", logCollectTransferProgress{})
	}
	if msg.TransferTotalSize > 0 && msg.TransferTotalSize > 25*1024*1024*1024 {
		return fail(logcontract.ErrTransferTooLarge, "日志包超过平台上限（25 GiB）", logCollectTransferProgress{})
	}
	partCount := msg.TransferPartCount
	if partCount <= 0 && msg.TransferTotalSize > 0 {
		partCount = int((msg.TransferTotalSize + chunkSize - 1) / chunkSize)
	}
	if partCount <= 0 {
		return fail(logcontract.ErrTransferChunkFailed, "part_count 无法确定", logCollectTransferProgress{})
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

	written, sum, err := PullArchive(sshx.NewSSHRunner(s), ctx, sourcePath, localPath, msg.TransferTotalSize, func(done, total int64) {
		progress.PulledBytes = done
		progress.TotalSize = total
		if total > 0 {
			progress.Percent = int(done * 50 / total)
		}
		emit("ok", progress, "", "")
	})
	progress.PulledBytes = written
	if err != nil {
		// 字节数不符是完整性错误（LC-TRANSFER-002），不是传输失败：
		// 错误码决定前端文案与是否自动重试，混用会让"包被截断"看起来像"网络抖动"。
		if errors.Is(err, ErrPullSizeMismatch) {
			return fail(logcontract.ErrTransferChecksum, err.Error(), progress)
		}
		return fail(logcontract.ErrTransferChunkFailed, err.Error(), progress)
	}

	info, err := os.Stat(localPath)
	if err != nil {
		return fail(logcontract.ErrTransferArchiveMissing, "本地副本不存在: "+err.Error(), progress)
	}
	if msg.TransferTotalSize > 0 && info.Size() != msg.TransferTotalSize {
		return fail(logcontract.ErrTransferChecksum, fmt.Sprintf("拉取后大小不一致：期望 %d，实际 %d", msg.TransferTotalSize, info.Size()), progress)
	}
	progress.Size = info.Size()
	// 摘要已在拉取过程中流式计算，这里不再重读整包。
	progress.Sha256 = sum

	progress.Stage = "uploading"
	uploaded := validUploadedParts(msg.TransferUploadedParts, partCount)
	progress.UploadedParts = len(uploaded)
	emit("ok", progress, "", "")

	done := len(uploaded)
	pushRes := uplink.Push(ctx, uplink.PushSpec{
		BaseURL:   platformURL,
		Prefix:    uplink.DirectUploadPrefix,
		TaskID:    taskID,
		Token:     token,
		LocalPath: localPath,
		ChunkSize: chunkSize,
		PartCount: partCount,
		TotalSize: progress.Size,
		Uploaded:  uploaded,
		Workers:   1,
		Client:    &http.Client{Timeout: uplink.DefaultPartTimeout},
		OnPart: func(_ int, _ int64) {
			done++
			progress.UploadedParts = done
			progress.Percent = 50 + int(done*50/partCount)
			emit("ok", progress, "", "")
		},
	})
	err = pushRes.Err
	if err == nil && ctx.Err() != nil {
		err = ctx.Err()
	}
	if err != nil {
		return fail(logcontract.ErrTransferChunkFailed, err.Error(), progress)
	}

	progress.Stage = "done"
	progress.Percent = 100
	emit("ok", progress, "", "")
	evidence.Emit("INFO", "log_collect.transfer_done", "日志包交付完成", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"task_id": taskID, "size": progress.Size, "sha256": progress.Sha256,
		"part_count": partCount, "local_path": localPath,
	})
	return nil
}
