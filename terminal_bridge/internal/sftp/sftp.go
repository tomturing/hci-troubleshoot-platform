package sftp

// SFTP 文件下载（HCI 远端 → terminal_bridge 所在机器）。
//
// 第一性原理（为什么是这个形状）：
//  1. 浏览器连不到目标主机，"能碰到 HCI 文件系统"的只有桥接进程；而用户要的落点是
//     **bridge 所在机器的磁盘**（桌面模式=工程师本人电脑，集群模式=Pod 的持久卷），
//     因此字节流根本不经过浏览器：WebSocket 只承载目录列表、进度、结果这类元数据。
//  2. SFTP 是 SSH 传输层上的独立 channel，可直接复用已认证的 *ssh.Client，
//     不需要二次认证，也不会和交互式 shell 抢同一个 session。
//  3. 落盘路径必须由 bridge 决定，绝不接受前端指定的绝对路径：桌面模式 Origin=*，
//     任意本地网页都能连 ws://localhost:9999，一旦允许指定路径就等于把"任意文件写"
//     开放给任何页面。
//  4. 远端可能禁用 sftp subsystem（部分加固过的设备），此时回退用 `tail -c +N` 走
//     exec 通道续拉——这条路径在 log_collect 的制品回传里已被现场验证。
//
// 对抗性审查（本实现刻意规避的坑）：
//   - 远端文件名可以包含 `/`、`..`、控制字符、Windows 保留名（CON/NUL…），
//     一律先清洗成安全 basename，再在目标目录内做唯一化，杜绝本地路径穿越/覆盖；
//   - 下载写 `.part` 临时文件，校验通过后原子 rename：中断不会留下"看起来完整"的半个文件；
//   - 单文件与并发都有上限，避免一条 20 GiB 的流把客户到 HCI 的链路打满、终端一起卡住；
//   - 每个任务都能取消，WebSocket 断开时按连接回收，防止 goroutine 与远程 channel 泄漏。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	remoteutil "terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/sshx"
	"time"
	"unicode"

	"golang.org/x/crypto/ssh"
	"golang.org/x/net/websocket"
)

// SFTP 专用错误短名。
//
// 与 vm_console 保持同一约定：短名进 error_type 供前端分支，message 给人看；
// 不登记进 log_collect 的 LC-* 错误码目录——那是日志收集链路的单一真源，混用会污染语义。
const (
	sftpErrSessionMissing = "session_missing"
	sftpErrInvalidPath    = "invalid_path"
	sftpErrInvalidTask    = "invalid_task"
	sftpErrInvalidAction  = "invalid_action"
	sftpErrNotFile        = "not_file"
	sftpErrTooLarge       = "too_large"
	sftpErrLocalIO        = "local_io_failed"
	sftpErrRemoteIO       = "remote_io_failed"
	sftpErrSizeMismatch   = "size_mismatch"
	sftpErrBusy           = "too_many_tasks"
	sftpErrDuplicateTask  = "duplicate_task"
	sftpErrCancelled      = "cancelled"
	sftpErrUnavailable    = "sftp_unavailable"
)

const (
	// 目录列表上限：HCI 的 /sf/data 下动辄上万条目，全量回包会撑爆 WS 帧并拖死 UI。
	sftpListMaxEntries = 2000
	// 单文件上限：默认 5 GiB。超过基本是误点（镜像/磁盘镜像），应走专门的搬运方案。
	sftpDefaultMaxBytes = int64(5) << 30
	// 并发下载上限：全局 2 个。再多会挤占交互式 shell 的带宽与 SSH channel。
	sftpMaxConcurrentTasks = 2
	sftpProgressInterval   = 200 * time.Millisecond
	sftpCopyBufferSize     = 512 * 1024
	sftpMaxRemotePathLen   = 4096
	sftpMaxLocalNameRunes  = 120
)

// sftpEntry 是回给前端的一行目录项。
type sftpEntry struct {
	Name    string `json:"name"`
	Path    string `json:"path"`
	IsDir   bool   `json:"is_dir"`
	Size    int64  `json:"size"`
	ModTime string `json:"mod_time,omitempty"`
	Mode    string `json:"mode,omitempty"`
}

// sftpListPayload 是 sftp_list_result 的结构化负载。
type sftpListPayload struct {
	Path string `json:"path"`
	// Parent 供 UI 的"上一级"按钮使用，根目录为空串。
	Parent string `json:"parent"`
	// DownloadDir 告诉用户文件最终落在本机哪个目录——这是本功能唯一的"交付凭据"。
	DownloadDir string      `json:"download_dir"`
	Entries     []sftpEntry `json:"entries"`
	Truncated   bool        `json:"truncated"`
}

// sftpTransferPayload 是进度/结果共用的结构化负载。
type sftpTransferPayload struct {
	TaskID     string `json:"task_id"`
	Phase      string `json:"phase"` // preparing | downloading | done
	RemotePath string `json:"remote_path"`
	LocalPath  string `json:"local_path,omitempty"`
	Bytes      int64  `json:"bytes"`
	TotalBytes int64  `json:"total_bytes"`
	Percent    int    `json:"percent"`
	// Transport 说明字节是怎么拉下来的：sftp 或 exec_fallback（sftp subsystem 不可用）。
	Transport string `json:"transport,omitempty"`
	// Sha256 只在完整下载后给出；续传时会先把已有片段重新摘要一次，保证与远端一致。
	Sha256        string `json:"sha256,omitempty"`
	Status        string `json:"status,omitempty"` // ok | error | cancelled | exists
	DownloadDir   string `json:"download_dir,omitempty"`
	DurationMSecs int64  `json:"duration_ms,omitempty"`
}

// dropSftpRemoteClientIfIdle 在没有其他并发任务时才丢弃通道。
//
// 关闭通道会连带干掉同会话上正在跑的另一个下载，所以必须先确认没有旁人。
// allowSelf=true 表示调用方自己已登记在任务表里（下载），计数为 1 属正常。
func dropSftpRemoteClientIfIdle(b *app.Bridge, session *sshx.SSHSession, allowSelf bool) bool {
	running := runningSftpTasks(b)
	if allowSelf {
		if running > 1 {
			return false
		}
	} else if running > 0 {
		return false
	}
	session.DropSftpRemoteClient()
	return true
}

// ── 任务登记 ──────────────────────────────────────────────────────────────────

type sftpTask struct {
	cancel context.CancelFunc
	connID string
	caseID string
}

func sftpTaskKey(caseID, taskID string) string {
	return caseID + "|" + taskID
}

// registerSftpTask 登记一个下载任务，并施加并发上限。
// 返回的 ctx 只由 cancel() 或连接断开触发，不设总超时：大文件下载耗时不可预估，
// 用固定超时会变成"越大越容易失败"的反向激励。
func registerSftpTask(b *app.Bridge, key, connID, caseID string) (context.Context, context.CancelFunc, string) {
	b.SftpTasksMu.Lock()
	defer b.SftpTasksMu.Unlock()
	if b.SftpTasks == nil {
		b.SftpTasks = make(map[string]any)
	}
	if _, exists := b.SftpTasks[key]; exists {
		return nil, nil, sftpErrDuplicateTask
	}
	if len(b.SftpTasks) >= sftpMaxConcurrentTasks {
		return nil, nil, sftpErrBusy
	}
	ctx, cancel := context.WithCancel(context.Background())
	b.SftpTasks[key] = &sftpTask{cancel: cancel, connID: connID, caseID: caseID}
	return ctx, cancel, ""
}

func finishSftpTask(b *app.Bridge, key string) {
	b.SftpTasksMu.Lock()
	defer b.SftpTasksMu.Unlock()
	delete(b.SftpTasks, key)
}

func cancelSftpTask(b *app.Bridge, key string) bool {
	b.SftpTasksMu.Lock()
	defer b.SftpTasksMu.Unlock()
	v, ok := b.SftpTasks[key]
	if !ok {
		return false
	}
	v.(*sftpTask).cancel()
	return true
}

// CancelSftpTasksForConn 在 WebSocket 断开时回收本连接发起的下载。
// 不回收的话，浏览器关掉后 goroutine 会继续把整个文件拉完，白占客户带宽。
func CancelSftpTasksForConn(b *app.Bridge, connID string) int {
	b.SftpTasksMu.Lock()
	defer b.SftpTasksMu.Unlock()
	cancelled := 0
	for key, v := range b.SftpTasks {
		task := v.(*sftpTask)
		if task.connID == connID {
			task.cancel()
			delete(b.SftpTasks, key)
			cancelled++
		}
	}
	return cancelled
}

func runningSftpTasks(b *app.Bridge) int {
	b.SftpTasksMu.Lock()
	defer b.SftpTasksMu.Unlock()
	return len(b.SftpTasks)
}

// ── 路径 / 本地落盘规划 ────────────────────────────────────────────────────────

// normalizeSftpRemotePath 校验远端路径，返回规范绝对路径或错误短名。
// 远端永远是 POSIX 语义，因此这里用 path 而非 filepath。
func normalizeSftpRemotePath(raw string) (string, string, string) {
	value := strings.TrimSpace(raw)
	if value == "" {
		return "", sftpErrInvalidPath, "远端路径不能为空"
	}
	if len(value) > sftpMaxRemotePathLen {
		return "", sftpErrInvalidPath, "远端路径过长"
	}
	if strings.ContainsAny(value, "\x00\n\r\t") {
		return "", sftpErrInvalidPath, "远端路径包含非法控制字符"
	}
	if !strings.HasPrefix(value, "/") {
		return "", sftpErrInvalidPath, "请使用绝对路径（以 / 开头）"
	}
	cleaned := path.Clean(value)
	if cleaned == "/" {
		return cleaned, "", ""
	}
	return cleaned, "", ""
}

// resolveSftpDownloadDir 决定本机落地目录，按"显式配置 → 用户下载目录 → 持久卷 → 临时目录"探测。
//
// 返回的目录一定是已创建且可写的第一项：集群模式下 Pod 以非 root 运行，
// $HOME 往往不可写，此时必须落到持久卷，否则文件写不出来而用户只看到一句 IO 失败。
func resolveSftpDownloadDir() (string, error) {
	candidates := make([]string, 0, 4)
	if configured := strings.TrimSpace(os.Getenv("HCI_BRIDGE_SFTP_DIR")); configured != "" {
		candidates = append(candidates, configured)
	}
	if home, err := os.UserHomeDir(); err == nil && strings.TrimSpace(home) != "" {
		candidates = append(candidates, filepath.Join(home, "Downloads", "hci-sftp"))
	}
	candidates = append(candidates, filepath.Join(string(filepath.Separator)+"var", "lib", "terminal-bridge", "downloads"))
	candidates = append(candidates, filepath.Join(os.TempDir(), "hci-sftp"))

	var lastErr error
	for _, candidate := range candidates {
		if err := os.MkdirAll(candidate, 0o700); err != nil {
			lastErr = err
			continue
		}
		return candidate, nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("没有可用的落地目录")
	}
	return "", lastErr
}

// sanitizeSftpLocalName 把远端文件名压成一个安全的本地文件名。
//
// 远端文件名是攻击面：`../../etc/passwd`、含 NUL/换行的名字、Windows 保留设备名
// （CON/NUL/COM1…）都会在落盘时变成越权写入或写失败。这里一律清洗，不接受任何路径成分。
func sanitizeSftpLocalName(remoteBase string) string {
	base := strings.TrimSpace(remoteBase)
	base = path.Base(base)
	var builder strings.Builder
	for _, r := range base {
		switch {
		case r == '/' || r == '\\' || r == ':' || r == '*' || r == '?' || r == '"' ||
			r == '<' || r == '>' || r == '|' || unicode.IsControl(r):
			builder.WriteRune('_')
		default:
			builder.WriteRune(r)
		}
	}
	name := builder.String()
	name = strings.Trim(name, " ")
	// Windows 不允许文件名以点或空格结尾（写入会静默变成别的名字）。
	name = strings.TrimRight(name, ". ")
	if name == "" || name == "." || name == ".." {
		return "download.bin"
	}
	// 保留扩展名做长度截断，避免把后缀截掉导致文件无法双击打开。
	if runes := []rune(name); len(runes) > sftpMaxLocalNameRunes {
		ext := path.Ext(name)
		if len([]rune(ext)) > 20 {
			ext = ""
		}
		keep := sftpMaxLocalNameRunes - len([]rune(ext))
		name = string(runes[:keep]) + ext
	}
	if isWindowsReservedName(name) {
		name = "_" + name
	}
	return name
}

func isWindowsReservedName(name string) bool {
	stem := strings.ToUpper(strings.TrimSuffix(name, path.Ext(name)))
	switch stem {
	case "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$":
		return true
	}
	if len(stem) == 4 {
		prefix := stem[:3]
		if prefix == "COM" || prefix == "LPT" {
			return stem[3] >= '1' && stem[3] <= '9'
		}
	}
	return false
}

// sftpLocalTarget 描述一次下载的本地落点。
type sftpLocalTarget struct {
	FinalPath string
	PartPath  string
	// ResumeFrom 是已下载字节数；>0 表示从该位置续传。
	ResumeFrom int64
	// AlreadyComplete 表示目标文件已完整存在，无需再拉一次（重试/重复点击幂等）。
	AlreadyComplete bool
}

// planSftpLocalTarget 在落地目录内挑选不与用户已有文件冲突的名字。
//
// 刻意不覆盖同名文件：用户的下载目录里可能有同名但内容不同的文件，
// 覆盖是不可逆的数据损失，而"多一个 -2 后缀"只是稍微难看一下。
func planSftpLocalTarget(dir, remotePath string, totalSize int64) (sftpLocalTarget, error) {
	base := sanitizeSftpLocalName(remotePath)
	if strings.TrimSpace(dir) == "" {
		return sftpLocalTarget{}, fmt.Errorf("落地目录为空")
	}
	var firstCandidate string
	for index := 1; index <= 200; index++ {
		name := base
		if index > 1 {
			ext := path.Ext(base)
			stem := strings.TrimSuffix(base, ext)
			name = fmt.Sprintf("%s-%d%s", stem, index, ext)
		}
		finalPath := filepath.Join(dir, name)
		if index == 1 {
			firstCandidate = finalPath
		}
		if info, err := os.Stat(finalPath); err == nil {
			// 同名且大小一致：判定为"上次已经下完"，直接幂等返回。
			if index == 1 && totalSize > 0 && info.Size() == totalSize {
				return sftpLocalTarget{FinalPath: finalPath, AlreadyComplete: true}, nil
			}
			continue
		}
		partPath := finalPath + ".part"
		resumeFrom := int64(0)
		if info, err := os.Stat(partPath); err == nil {
			resumeFrom = info.Size()
			// 残留片段比远端还大，说明是另一个文件的碎片：丢弃重来。
			if totalSize > 0 && resumeFrom > totalSize {
				_ = os.Remove(partPath)
				resumeFrom = 0
			}
		}
		return sftpLocalTarget{FinalPath: finalPath, PartPath: partPath, ResumeFrom: resumeFrom}, nil
	}
	if firstCandidate == "" {
		return sftpLocalTarget{}, fmt.Errorf("无法分配本地文件名")
	}
	return sftpLocalTarget{}, fmt.Errorf("同名文件过多（%s），请先清理下载目录", filepath.Base(firstCandidate))
}

func sftpMaxBytes() int64 {
	configured := strings.TrimSpace(os.Getenv("HCI_BRIDGE_SFTP_MAX_BYTES"))
	if configured != "" {
		if parsed, err := strconv.ParseInt(configured, 10, 64); err == nil && parsed > 0 {
			return parsed
		}
	}
	return sftpDefaultMaxBytes
}

// ── 消息入口 ──────────────────────────────────────────────────────────────────

// HandleSftpOp 是 sftp_op 的总入口。所有分支都收敛在这里，避免协议散落。
func HandleSftpOp(b *app.Bridge, ws *websocket.Conn, msg models.InMessage, cui, connID string) {
	traceID := strings.TrimSpace(msg.TraceID)
	if traceID == "" {
		traceID = "sftp-" + logcollect.FirstNonEmpty(strings.TrimSpace(msg.ExecID), "unknown")
	}
	msg.TraceID = traceID

	switch strings.TrimSpace(msg.SftpAction) {
	case "list":
		handleSftpList(b, ws, msg, cui)
	case "download":
		handleSftpDownload(b, ws, msg, cui, connID)
	case "cancel":
		handleSftpCancel(b, ws, msg, cui)
	default:
		obslog.Emit("ERROR", "sftp.rejected", "sftp_op 动作非法，已拒绝", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"sftp_action": msg.SftpAction, "task_id": msg.ExecID,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: msg.ExecID, SftpAction: "unknown",
			ErrorType: sftpErrInvalidAction, Message: "不支持的 sftp 动作: " + msg.SftpAction,
			TraceID: traceID, CustomUI: cui,
		})
	}
}

func handleSftpList(b *app.Bridge, ws *websocket.Conn, msg models.InMessage, cui string) {
	// 空路径表示"我不知道在哪，给我默认目录"：由服务端解析成远端 home，
	// 前端因此可以一打开面板就发请求，无需先猜一个起始路径。
	wantsDefaultDir := strings.TrimSpace(msg.SftpPath) == ""
	remotePath, errType, errMsg := normalizeSftpRemotePath(msg.SftpPath)
	if errType != "" && !wantsDefaultDir {
		obslog.Emit("ERROR", "sftp.rejected", "目录列表请求被拒绝", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"error_type": errType, "reason": errMsg, "path_len": len(msg.SftpPath),
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, SftpAction: "list",
			ErrorType: errType, Message: errMsg, TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}

	session, key, match := logcollect.ResolveLogCollectSession(b, msg)
	if session == nil {
		// 与日志收集同一套定性字段：会话数为 0 是"连接生命周期已结束"，
		// 会话数 >0 但键对不上是路由问题，二者的处置完全不同。
		obslog.Emit("ERROR", "sftp.session_missing", "目录列表缺少 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"sftp_action": "list", "requested_key": app.SessionKey(msg.CaseID, msg.NodeIP),
			"active_sessions": b.SessionCount(), "session_keys": b.SessionKeyList(8),
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, SftpAction: "list",
			ErrorType: sftpErrSessionMissing, Message: "SSH 会话不存在（需先连接终端）",
			TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}
	if match != "exact" {
		obslog.Emit("INFO", "sftp.session_fallback", "文件下载回退复用 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"sftp_action": "list", "session_key": key, "match": match,
		})
	}

	downloadDir, dirErr := resolveSftpDownloadDir()
	if dirErr != nil {
		downloadDir = ""
	}

	remote, sftpErr := session.SftpRemoteClient()
	if sftpErr != nil {
		// 目录浏览依赖 sftp subsystem；不可用时明确降级提示，而不是让用户对着空列表猜。
		obslog.Emit("ERROR", "sftp.unavailable", "SFTP 子系统不可用，目录列表失败", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"error": sftpErr.Error(), "path": remotePath, "session_key": key,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, SftpAction: "list",
			ErrorType: sftpErrUnavailable,
			Message:   "当前节点未启用 SFTP 子系统，无法浏览目录；可直接输入文件绝对路径下载（将回退到 exec 通道）",
			Detail:    sftpErr.Error(), TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}

	if wantsDefaultDir {
		remotePath = sftpDefaultRemoteDir(remote)
	}

	entries, truncated, listErr := listSftpDirectory(remote, remotePath, sftpListMaxEntries)
	if listErr != nil {
		// 列表失败时丢弃通道：死通道会让用户“刷新多少次都报同样的错”。
		dropSftpRemoteClientIfIdle(b, session, false)
		obslog.Emit("ERROR", "sftp.list_failed", "读取远端目录失败", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"error": listErr.Error(), "path": remotePath, "session_key": key,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, SftpAction: "list",
			ErrorType: sftpErrRemoteIO, Message: "读取远端目录失败: " + listErr.Error(),
			TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}

	obslog.Emit("INFO", "sftp.list", "读取远端目录", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"path": remotePath, "entries": len(entries), "truncated": truncated,
		"download_dir": downloadDir, "session_key": key,
	})
	models.SendMsg(ws, models.OutMessage{
		Type: "sftp_list_result", CaseID: msg.CaseID, SftpAction: "list", ExecID: msg.ExecID,
		TraceID: msg.TraceID, CustomUI: cui,
		SftpData: sftpListPayload{
			Path:        remotePath,
			Parent:      sftpParentPath(remotePath),
			DownloadDir: downloadDir,
			Entries:     entries,
			Truncated:   truncated,
		},
	})
}

// sftpDefaultRemoteDir 取远端登录用户的初始目录，用于“打开面板时不知道路径”的场景。
//
// Getwd 失败不报错：部分受限账号 / chroot 环境不实现 realpath，
// 此时回落根目录仍能给出可用列表，比直接报错让面板空转有用。
func sftpDefaultRemoteDir(remote sshx.SftpRemote) string {
	if wd, err := remote.Getwd(); err == nil {
		if cleaned, _, _ := normalizeSftpRemotePath(wd); cleaned != "" {
			return cleaned
		}
	}
	return "/"
}

// listSftpDirectory 把远端目录项转成前端结构：目录在前、同类按名称排序。
func listSftpDirectory(remote sshx.SftpRemote, remotePath string, maxEntries int) ([]sftpEntry, bool, error) {
	infos, err := remote.ListDir(remotePath)
	if err != nil {
		return nil, false, err
	}
	entries := make([]sftpEntry, 0, len(infos))
	for _, info := range infos {
		name := info.Name()
		if name == "." || name == ".." {
			continue
		}
		size := info.Size()
		if info.IsDir() {
			size = 0
		}
		entries = append(entries, sftpEntry{
			Name:    name,
			Path:    path.Join(remotePath, name),
			IsDir:   info.IsDir(),
			Size:    size,
			ModTime: info.ModTime().UTC().Format(time.RFC3339),
			Mode:    info.Mode().String(),
		})
	}
	sort.SliceStable(entries, func(i, j int) bool {
		if entries[i].IsDir != entries[j].IsDir {
			return entries[i].IsDir
		}
		return strings.ToLower(entries[i].Name) < strings.ToLower(entries[j].Name)
	})
	if maxEntries > 0 && len(entries) > maxEntries {
		return entries[:maxEntries], true, nil
	}
	return entries, false, nil
}

func sftpParentPath(remotePath string) string {
	if remotePath == "/" {
		return ""
	}
	parent := path.Dir(remotePath)
	if parent == "." {
		return "/"
	}
	return parent
}

func handleSftpCancel(b *app.Bridge, ws *websocket.Conn, msg models.InMessage, cui string) {
	taskID := strings.TrimSpace(msg.ExecID)
	if taskID == "" {
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, SftpAction: "cancel",
			ErrorType: sftpErrInvalidTask, Message: "缺少任务 ID", TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}
	key := sftpTaskKey(msg.CaseID, taskID)
	if !cancelSftpTask(b, key) {
		// 找不到任务不是错误：任务可能刚刚自然结束，前端据此把按钮复位即可。
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "cancel",
			TraceID: msg.TraceID, CustomUI: cui,
			SftpData: sftpTransferPayload{TaskID: taskID, Phase: "done", Status: "cancelled"},
			Message:  "任务已结束，无需取消",
		})
		return
	}
	obslog.Emit("INFO", "sftp.cancel_requested", "已请求取消下载任务", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"task_id": taskID,
	})
}

// handleSftpDownload 执行「HCI 远端 → 本机磁盘」的单文件下载。
func handleSftpDownload(b *app.Bridge, ws *websocket.Conn, msg models.InMessage, cui, connID string) {
	taskID := strings.TrimSpace(msg.ExecID)
	if taskID == "" {
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, SftpAction: "download",
			ErrorType: sftpErrInvalidTask, Message: "缺少任务 ID（exec_id）", TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}

	remotePath, errType, errMsg := normalizeSftpRemotePath(msg.SftpPath)
	if errType != "" {
		obslog.Emit("ERROR", "sftp.rejected", "下载请求被拒绝", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"error_type": errType, "reason": errMsg, "task_id": taskID, "path_len": len(msg.SftpPath),
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
			ErrorType: errType, Message: errMsg, TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}

	session, key, match := logcollect.ResolveLogCollectSession(b, msg)
	if session == nil {
		obslog.Emit("ERROR", "sftp.session_missing", "下载任务缺少 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "requested_key": app.SessionKey(msg.CaseID, msg.NodeIP),
			"active_sessions": b.SessionCount(), "session_keys": b.SessionKeyList(8),
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
			ErrorType: sftpErrSessionMissing, Message: "SSH 会话不存在（需先连接终端）",
			TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}

	taskKey := sftpTaskKey(msg.CaseID, taskID)
	ctx, cancel, registerErr := registerSftpTask(b, taskKey, connID, msg.CaseID)
	if registerErr != "" {
		message := "并发下载任务已达上限（" + strconv.Itoa(sftpMaxConcurrentTasks) + " 个），请等待当前任务结束"
		if registerErr == sftpErrDuplicateTask {
			message = "同一任务正在下载中，请勿重复提交"
		}
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
			ErrorType: registerErr, Message: message, TraceID: msg.TraceID, CustomUI: cui,
		})
		return
	}
	defer func() {
		cancel()
		finishSftpTask(b, taskKey)
	}()
	if match != "exact" {
		obslog.Emit("INFO", "sftp.session_fallback", "下载任务回退复用 SSH 会话", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "session_key": key, "match": match,
		})
	}

	startedAt := time.Now()
	emit := func(payload sftpTransferPayload) {
		payload.TaskID = taskID
		payload.RemotePath = remotePath
		payload.DurationMSecs = time.Since(startedAt).Milliseconds()
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_transfer_progress", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
			TraceID: msg.TraceID, CustomUI: cui, SftpData: payload,
		})
	}
	fail := func(errType, message string, payload sftpTransferPayload) {
		payload.Phase = "done"
		payload.Status = "error"
		atomic.AddUint64(&app.PromMetrics.SftpDownloadErrors, 1)
		if payload.Percent >= 100 {
			payload.Percent = 99
		}
		emit(payload)
		obslog.Emit("ERROR", "sftp.download_failed", "文件下载失败", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "remote_path": remotePath, "error_type": errType, "error": message,
			"bytes": payload.Bytes, "total_bytes": payload.TotalBytes,
			"local_path": payload.LocalPath, "transport": payload.Transport,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
			ErrorType: errType, Message: message, TraceID: msg.TraceID, CustomUI: cui,
			SftpData: payload,
		})
	}

	downloadDir, dirErr := resolveSftpDownloadDir()
	if dirErr != nil {
		fail(sftpErrLocalIO, "本机下载目录不可用: "+dirErr.Error(), sftpTransferPayload{})
		return
	}

	emit(sftpTransferPayload{Phase: "preparing", DownloadDir: downloadDir})

	maxBytes := sftpMaxBytes()
	transport := "sftp"
	var totalSize int64

	remote, sftpErr := session.SftpRemoteClient()
	if sftpErr == nil {
		info, statErr := remote.Stat(remotePath)
		if statErr != nil {
			dropSftpRemoteClientIfIdle(b, session, true)
			fail(sftpErrRemoteIO, "读取远端文件属性失败: "+statErr.Error(), sftpTransferPayload{Transport: transport})
			return
		}
		if info.IsDir() {
			fail(sftpErrNotFile, "目标是目录，请展开目录后选择文件", sftpTransferPayload{Transport: transport})
			return
		}
		totalSize = info.Size()
	} else {
		// SFTP subsystem 被禁用：用 shell 探测属性并改走 exec 流。
		transport = "exec_fallback"
		size, isDir, probeErr := remoteFileSize(session, ctx, remotePath)
		if probeErr != nil {
			fail(sftpErrRemoteIO, "读取远端文件属性失败: "+probeErr.Error(), sftpTransferPayload{Transport: transport})
			return
		}
		if isDir {
			fail(sftpErrNotFile, "目标是目录；当前节点的 SFTP 子系统不可用，无法浏览目录，请直接输入文件绝对路径", sftpTransferPayload{Transport: transport})
			return
		}
		totalSize = size
		obslog.Emit("INFO", "sftp.exec_fallback", "SFTP 子系统不可用，回退 exec 通道下载", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "path": remotePath, "size": totalSize, "sftp_error": sftpErr.Error(),
		})
	}

	if totalSize > maxBytes {
		fail(sftpErrTooLarge, fmt.Sprintf("文件大小 %s 超过上限 %s", remoteutil.FormatBytesForLog(totalSize), remoteutil.FormatBytesForLog(maxBytes)),
			sftpTransferPayload{TotalBytes: totalSize, Transport: transport})
		return
	}

	target, planErr := planSftpLocalTarget(downloadDir, remotePath, totalSize)
	if planErr != nil {
		fail(sftpErrLocalIO, planErr.Error(), sftpTransferPayload{TotalBytes: totalSize, Transport: transport})
		return
	}
	if target.AlreadyComplete {
		payload := sftpTransferPayload{
			Phase: "done", Status: "exists", LocalPath: target.FinalPath,
			Bytes: totalSize, TotalBytes: totalSize, Percent: 100,
			Transport: transport, DownloadDir: downloadDir,
		}
		emit(payload)
		obslog.Emit("INFO", "sftp.download_exists", "本地已存在同名同大小文件，跳过下载", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"task_id": taskID, "remote_path": remotePath, "local_path": target.FinalPath, "size": totalSize,
		})
		models.SendMsg(ws, models.OutMessage{
			Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
			TraceID: msg.TraceID, CustomUI: cui, SftpData: payload,
			Message: "本机已存在同名同大小文件，已跳过",
		})
		return
	}

	atomic.AddUint64(&app.PromMetrics.SftpDownloadsTotal, 1)
	obslog.Emit("INFO", "sftp.download_start", "开始下载远端文件", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"task_id": taskID, "remote_path": remotePath, "local_path": target.FinalPath,
		"size": totalSize, "resume_from": target.ResumeFrom, "transport": transport,
	})

	var src io.Reader
	var closeSrc func()
	if transport == "sftp" {
		file, openErr := remote.Open(remotePath)
		if openErr != nil {
			dropSftpRemoteClientIfIdle(b, session, true)
			fail(sftpErrRemoteIO, "打开远端文件失败: "+openErr.Error(), sftpTransferPayload{TotalBytes: totalSize, Transport: transport})
			return
		}
		if target.ResumeFrom > 0 {
			if _, seekErr := file.Seek(target.ResumeFrom, io.SeekStart); seekErr != nil {
				_ = file.Close()
				dropSftpRemoteClientIfIdle(b, session, true)
				fail(sftpErrRemoteIO, "远端续传定位失败: "+seekErr.Error(), sftpTransferPayload{TotalBytes: totalSize, Transport: transport})
				return
			}
		}
		src = file
		closeSrc = func() { _ = file.Close() }
	} else {
		stream, streamErr := openExecFileStream(session, ctx, remotePath, target.ResumeFrom)
		if streamErr != nil {
			fail(sftpErrRemoteIO, "打开远端文件失败: "+streamErr.Error(), sftpTransferPayload{TotalBytes: totalSize, Transport: transport})
			return
		}
		src = stream
		closeSrc = func() { _ = stream.Close() }
	}
	defer closeSrc()

	var lastReport time.Time
	written, sum, copyErr := copyRemoteToPart(ctx, src, target.PartPath, target.ResumeFrom, totalSize, func(done int64) {
		// 进度节流：每个 512 KiB 都发一条会在弱网上把 WS 灌满，反而拖慢真正的字节流。
		if time.Since(lastReport) < sftpProgressInterval && done < totalSize {
			return
		}
		lastReport = time.Now()
		emit(sftpTransferPayload{
			Phase: "downloading", LocalPath: target.PartPath, Bytes: done, TotalBytes: totalSize,
			Percent: percentOf(done, totalSize), Transport: transport,
		})
	})

	payload := sftpTransferPayload{
		RemotePath: remotePath, LocalPath: target.FinalPath, Bytes: written,
		TotalBytes: totalSize, Percent: percentOf(written, totalSize), Transport: transport,
	}
	if copyErr != nil {
		if ctx.Err() != nil {
			// 取消是用户的正常操作，不是故障：保留 .part 供下次续传。
			payload.Phase = "done"
			payload.Status = "cancelled"
			payload.LocalPath = target.PartPath
			emit(payload)
			obslog.Emit("INFO", "sftp.download_cancelled", "下载已取消", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"task_id": taskID, "remote_path": remotePath, "bytes": written,
				"partial_path": target.PartPath,
			})
			models.SendMsg(ws, models.OutMessage{
				Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
				ErrorType: sftpErrCancelled, Message: "下载已取消（已下载部分保留，可重试续传）",
				TraceID: msg.TraceID, CustomUI: cui, SftpData: payload,
			})
			return
		}
		fail(sftpErrRemoteIO, "下载中断: "+copyErr.Error(), payload)
		if transport == "sftp" {
			// 读到一半断掉基本是通道死了：丢弃缓存，让重试重新协商
			dropSftpRemoteClientIfIdle(b, session, true)
		}
		return
	}

	// 大小核对：远端 stat 与实收字节必须一致，否则宁可失败也不留一个"看起来完整"的坏文件。
	if totalSize > 0 && written != totalSize {
		fail(sftpErrSizeMismatch, fmt.Sprintf("下载大小不一致：远端 %d 字节，实收 %d 字节", totalSize, written), payload)
		return
	}
	if renameErr := os.Rename(target.PartPath, target.FinalPath); renameErr != nil {
		fail(sftpErrLocalIO, "本地落盘失败: "+renameErr.Error(), payload)
		return
	}

	payload.Phase = "done"
	payload.Status = "ok"
	payload.Percent = 100
	payload.Sha256 = sum
	payload.DownloadDir = downloadDir
	emit(payload)
	obslog.Emit("INFO", "sftp.download_done", "远端文件下载完成", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"task_id": taskID, "remote_path": remotePath, "local_path": target.FinalPath,
		"size": written, "sha256": sum, "transport": transport,
		"duration_ms": time.Since(startedAt).Milliseconds(),
	})
	models.SendMsg(ws, models.OutMessage{
		Type: "sftp_result", CaseID: msg.CaseID, ExecID: taskID, SftpAction: "download",
		TraceID: msg.TraceID, CustomUI: cui, SftpData: payload,
		Message: "下载完成",
	})
}

func percentOf(done, total int64) int {
	if total <= 0 {
		return 0
	}
	percent := int(done * 100 / total)
	if percent > 100 {
		percent = 100
	}
	if percent < 0 {
		percent = 0
	}
	return percent
}

// copyRemoteToPart 把远端流写进 .part 文件，同时计算 SHA-256。
//
// 续传时先把已有片段重新摘要一遍：跳过它会让最终哈希与文件内容不符，
// 那比不提供哈希更糟——用户会以为校验通过。
func copyRemoteToPart(
	ctx context.Context,
	src io.Reader,
	partPath string,
	resumeFrom int64,
	totalSize int64,
	onProgress func(written int64),
) (int64, string, error) {
	file, err := os.OpenFile(partPath, os.O_WRONLY|os.O_CREATE, 0o600)
	if err != nil {
		return 0, "", err
	}
	defer file.Close()

	if err := file.Truncate(resumeFrom); err != nil {
		return 0, "", err
	}
	digest := sha256.New()
	if resumeFrom > 0 {
		if err := hashFilePrefix(partPath, resumeFrom, digest); err != nil {
			return 0, "", err
		}
	}
	if _, err := file.Seek(resumeFrom, io.SeekStart); err != nil {
		return resumeFrom, "", err
	}

	written := resumeFrom
	buffer := make([]byte, sftpCopyBufferSize)
	for {
		if ctx.Err() != nil {
			_ = file.Sync()
			return written, hex.EncodeToString(digest.Sum(nil)), ctx.Err()
		}
		read, readErr := src.Read(buffer)
		if read > 0 {
			if _, writeErr := file.Write(buffer[:read]); writeErr != nil {
				return written, hex.EncodeToString(digest.Sum(nil)), writeErr
			}
			_, _ = digest.Write(buffer[:read])
			written += int64(read)
			if onProgress != nil {
				onProgress(written)
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return written, hex.EncodeToString(digest.Sum(nil)), readErr
		}
	}
	if err := file.Sync(); err != nil {
		return written, hex.EncodeToString(digest.Sum(nil)), err
	}
	return written, hex.EncodeToString(digest.Sum(nil)), nil
}

func hashFilePrefix(filePath string, length int64, digest io.Writer) error {
	file, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := io.CopyN(digest, file, length); err != nil && err != io.EOF {
		return err
	}
	return nil
}

// ── exec 回退通道（SFTP 子系统不可用）─────────────────────────────────────────

// remoteFileSize 用 shell 探测远端路径：区分"目录 / 缺失 / 普通文件 + 字节数"。
func remoteFileSize(s *sshx.SSHSession, ctx context.Context, remotePath string) (int64, bool, error) {
	quoted := logcollect.ShellQuote(remotePath)
	command := fmt.Sprintf(
		"if [ -d %s ]; then echo __HCI_DIR__; elif [ -f %s ]; then stat -c %%s %s; else echo __HCI_MISSING__; fi",
		quoted, quoted, quoted,
	)
	output, exitCode, err := s.RunQuickCommand(ctx, command)
	if err != nil && exitCode < 0 {
		return 0, false, err
	}
	switch strings.TrimSpace(output) {
	case "__HCI_DIR__":
		return 0, true, nil
	case "__HCI_MISSING__":
		return 0, false, fmt.Errorf("远端文件不存在或无权限访问")
	}
	if exitCode != 0 {
		return 0, false, fmt.Errorf("stat 失败(exit=%d): %s", exitCode, strings.TrimSpace(output))
	}
	size, parseErr := strconv.ParseInt(strings.TrimSpace(output), 10, 64)
	if parseErr != nil {
		return 0, false, fmt.Errorf("无法解析文件大小: %s", strings.TrimSpace(output))
	}
	return size, false, nil
}

// execFileStream 是 `tail -c +N <file>` 的输出流；Close 会结束远端命令。
//
// 注意：ssh.Session.StdoutPipe() 返回的是 io.Reader（不可单独关闭），
// 结束远端命令的唯一手段是关闭 Session。
type execFileStream struct {
	stdout  io.Reader
	session *ssh.Session
	once    sync.Once
}

func (s *execFileStream) Read(p []byte) (int, error) { return s.stdout.Read(p) }

func (s *execFileStream) Close() error {
	var closeErr error
	s.once.Do(func() {
		closeErr = s.session.Close()
	})
	return closeErr
}

// openExecFileStream 从 offset 处开始读远端文件（tail 的 -c +N 是 1-based）。
func openExecFileStream(s *sshx.SSHSession, ctx context.Context, remotePath string, offset int64) (io.ReadCloser, error) {
	s.Mu.Lock()
	client := s.Client
	s.Mu.Unlock()
	if client == nil {
		return nil, fmt.Errorf("SSH 会话已关闭")
	}
	session, err := client.NewSession()
	if err != nil {
		return nil, err
	}
	stdout, err := session.StdoutPipe()
	if err != nil {
		_ = session.Close()
		return nil, err
	}
	command := fmt.Sprintf("tail -c +%d %s", offset+1, logcollect.ShellQuote(remotePath))
	if err := session.Start(command); err != nil {
		_ = session.Close()
		return nil, err
	}
	stream := &execFileStream{stdout: stdout, session: session}
	// 取消时必须主动关会话：Read 阻塞在管道上，不关会话就永远等不到返回。
	// 该 goroutine 随任务 ctx 结束退出（任务收尾一定会 cancel）。
	go func() {
		<-ctx.Done()
		_ = session.Close()
	}()
	return stream, nil
}
