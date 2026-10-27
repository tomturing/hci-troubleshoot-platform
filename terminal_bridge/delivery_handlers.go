package main

// 交付作业的 WS 入口：submit / status / resume / cancel。
//
// 与旧 `transfer` 的差别：旧实现让前端 `sendAndWait` 等满 2 小时，断线即不可恢复；
// 新实现立刻返回 job_id，前端改为轮询 status。执行权在 bridge，页面关掉也继续。
// 旧 `transfer` 保留为兼容路径（老前端仍可用）。

import (
	"strings"
	"time"

	"golang.org/x/net/websocket"
)

// deliveryJobView 是回给前端的快照：字段名与前端约定保持一致。
type deliveryJobView struct {
	JobID         string `json:"job_id"`
	TaskID        string `json:"task_id"`
	Phase         string `json:"phase"`
	Progress      int    `json:"progress"`
	SourceSize    int64  `json:"source_size"`
	PulledBytes   int64  `json:"pulled_bytes"`
	PartCount     int    `json:"part_count"`
	UploadedParts int    `json:"uploaded_parts"`
	Attempt       int    `json:"attempt"`
	NeedsReauth   bool   `json:"needs_reauth"`
	LastError     string `json:"last_error"`
}

func toDeliveryJobView(job *deliveryJob) deliveryJobView {
	if job == nil {
		return deliveryJobView{}
	}
	return deliveryJobView{
		JobID: job.JobID, TaskID: job.TaskID, Phase: string(job.Phase),
		Progress: job.progress(), SourceSize: job.SourceSize, PulledBytes: job.PulledBytes,
		PartCount: job.PartCount, UploadedParts: len(job.UploadedParts),
		Attempt: job.Attempt, NeedsReauth: job.NeedsReauth, LastError: job.LastError,
	}
}

// handleDeliverySubmit 接收交付请求：校验参数、落盘、入队，立即返回 job_id。
func (b *Bridge) handleDeliverySubmit(ws *websocket.Conn, msg InMessage, cui string) {
	traceID := strings.TrimSpace(msg.TraceID)
	taskID := strings.TrimSpace(msg.ExecID)
	sourcePath, err := normalizeArchivePath(msg.TransferSourcePath)
	if err != nil {
		sendMsg(ws, OutMessage{
			Type: "delivery_result", CaseID: msg.CaseID, LogAction: "submit",
			Status: "error", ErrorType: "invalid_path", ErrorCode: ErrTransferArchiveMissing,
			Message: err.Error(), TraceID: traceID, CustomUI: cui,
		})
		return
	}
	platformURL, err := validatePlatformURL(msg.TransferPlatformURL)
	if err != nil {
		sendMsg(ws, OutMessage{
			Type: "delivery_result", CaseID: msg.CaseID, LogAction: "submit",
			Status: "error", ErrorType: "invalid_platform_url", ErrorCode: ErrTransferChunkFailed,
			Message: err.Error(), TraceID: traceID, CustomUI: cui,
		})
		return
	}
	if taskID == "" || strings.TrimSpace(msg.TransferToken) == "" {
		sendMsg(ws, OutMessage{
			Type: "delivery_result", CaseID: msg.CaseID, LogAction: "submit",
			Status: "error", ErrorType: "missing_params", ErrorCode: ErrTransferChunkFailed,
			Message: "task_id 与 upload_token 必填", TraceID: traceID, CustomUI: cui,
		})
		return
	}
	if deliveryRunnerInstance == nil {
		sendMsg(ws, OutMessage{
			Type: "delivery_result", CaseID: msg.CaseID, LogAction: "submit",
			Status: "error", ErrorType: "runner_unavailable", ErrorCode: ErrTransferChunkFailed,
			Message: "交付执行器未启动", TraceID: traceID, CustomUI: cui,
		})
		return
	}

	job := &deliveryJob{
		JobID:         "dj-" + taskID,
		TaskID:        taskID,
		CaseID:        msg.CaseID,
		NodeIP:        msg.NodeIP,
		SourcePath:    sourcePath,
		LocalPath:     strings.TrimSpace(msg.TransferLocalPath),
		PlatformURL:   platformURL,
		UploadToken:   strings.TrimSpace(msg.TransferToken),
		ChunkSize:     msg.TransferChunkSize,
		PartCount:     msg.TransferPartCount,
		SourceSize:    msg.TransferTotalSize,
		UploadedParts: append([]int(nil), msg.TransferUploadedParts...),
		Direct:        strings.EqualFold(strings.TrimSpace(msg.TransferDeliveryMode), deliveryModeDirect),
		TLSInsecure:   msg.TransferTLSInsecure,
	}
	if job.LastError == "" {
		job.LastError = ""
	}
	submitted := deliveryRunnerInstance.submit(job)

	blog("INFO", "delivery.submit", "收到交付作业请求", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"job_id": submitted.JobID, "task_id": taskID, "source_path": sourcePath,
		"part_count": job.PartCount, "chunk_size": job.ChunkSize, "direct": job.Direct,
	})
	sendMsg(ws, OutMessage{
		Type: "delivery_result", CaseID: msg.CaseID, LogAction: "submit", Status: "ok",
		Message: "已接管交付，浏览器可关闭", TraceID: traceID, CustomUI: cui,
		LogData: toDeliveryJobView(submitted),
	})
}

// handleDeliveryStatus 返回 job 快照；前端断线重连后靠它继续显示。
func (b *Bridge) handleDeliveryStatus(ws *websocket.Conn, msg InMessage, cui string) {
	jobID := strings.TrimSpace(msg.JobID)
	status := "error"
	view := deliveryJobView{}
	message := "作业不存在"
	if deliveryRunnerInstance != nil {
		if job := deliveryRunnerInstance.status(jobID); job != nil {
			status = "ok"
			message = ""
			view = toDeliveryJobView(job)
		}
	}
	sendMsg(ws, OutMessage{
		Type: "delivery_result", CaseID: msg.CaseID, LogAction: "status", Status: status,
		Message: message, TraceID: msg.TraceID, CustomUI: cui, LogData: view,
	})
}

// handleDeliveryResume 在用户重连后唤醒 needs_reauth 的作业。
func (b *Bridge) handleDeliveryResume(ws *websocket.Conn, msg InMessage, cui string) {
	jobID := strings.TrimSpace(msg.JobID)
	ok := deliveryRunnerInstance != nil && deliveryRunnerInstance.resume(jobID)
	status := "error"
	if ok {
		status = "ok"
	}
	blog("INFO", "delivery.resume", "请求恢复交付作业", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"job_id": jobID, "ok": ok,
	})
	sendMsg(ws, OutMessage{
		Type: "delivery_result", CaseID: msg.CaseID, LogAction: "resume", Status: status,
		Message: "已重新排队", TraceID: msg.TraceID, CustomUI: cui,
	})
}

// handleDeliveryCancel 取消作业（用户显式取消）。
func (b *Bridge) handleDeliveryCancel(ws *websocket.Conn, msg InMessage, cui string) {
	jobID := strings.TrimSpace(msg.JobID)
	ok := deliveryRunnerInstance != nil && deliveryRunnerInstance.cancel(jobID)
	blog("WARN", "delivery.cancel", "取消交付作业", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"job_id": jobID, "ok": ok,
	})
	status := "error"
	if ok {
		status = "ok"
	}
	sendMsg(ws, OutMessage{
		Type: "delivery_result", CaseID: msg.CaseID, LogAction: "cancel", Status: status,
		Message: "已取消", TraceID: msg.TraceID, CustomUI: cui,
	})
}

// deliveryProgressEvery 是进度回包的最小间隔（供前端轮询时判断是否值得刷新）。
const deliveryProgressEvery = 2 * time.Second
