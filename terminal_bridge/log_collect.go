package main

// 日志收集执行通道（acli log_collect）。
//
// 第一性原理：
//   1. 云端服务永远无法直连客户私网 HCI，执行只能发生在 bridge 的 SSH 会话内；
//   2. acli 插件命令在容器内 exec 时要求 stdin 为 tty，必须走 PTY（见 pty 改造）；
//   3. 采集是异步长任务，create 只负责下发；进度由 progress 轮询，且进度只反映
//      "发起节点"的本地视图，因此调用方必须把请求路由回同一节点；
//   4. 插件本身不做参数防注入（-s / -p 直接拼 shell），bridge 是最后一道防线，
//      所有外部输入必须白名单校验 + 单引号转义。

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/net/websocket"
)

const (
	logCollectDefaultLogServer = "http://acli.sangfor.com.cn:5888/"
	logCollectProbeTimeout     = 40 * time.Second
	logCollectCreateTimeout    = 180 * time.Second
	logCollectProgressTimeout  = 30 * time.Second
	logCollectCancelTimeout    = 180 * time.Second
	// acli log_collect -d 最多 30 天（插件与后端双重约束）。
	logCollectMaxRangeDays = 30
)

// logCollectModules 是 acli log_collect -m 的合法枚举（实机 acli 1.0.1 帮助）。
var logCollectModules = map[string]bool{
	"other":        true,
	"system":       true,
	"slowdisk":     true,
	"network":      true,
	"storage":      true,
	"vm_start":     true,
	"vm_suspend":   true,
	"host_offline": true,
	"performance":  true,
	"core":         true,
}

var (
	logCollectTicketRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,32}$`)
	logCollectDateRe   = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)
	logCollectIPv4Re   = regexp.MustCompile(`^(\d{1,3}\.){3}\d{1,3}$`)
	// 额外收集路径只允许绝对路径，并禁止 shell 元字符与空白（插件会直接拼命令）。
	logCollectPathRe     = regexp.MustCompile(`^/[A-Za-z0-9_./-]+$`)
	logCollectProgressRe = regexp.MustCompile(`^总进度：(\d+)% - (\d+)/(\d+)`)
	logCollectHostLineRe = regexp.MustCompile(`^(\d{1,3}(?:\.\d{1,3}){3})\s+(收集完成|收集中|收集失败)\s+(\d+%|-)`)
)

// shellQuote 用单引号包裹并按 POSIX 规则转义内部单引号。
// 插件的 -s/-p/-u 最终会被拼进 shell 命令，禁止直接使用用户输入。
func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

// buildLogCollectCreateCommand 校验参数并返回可直接执行的 acli 命令。
// 返回的 errType 非空表示拒绝执行，调用方应把原因回传前端。
func buildLogCollectCreateCommand(req InMessage) (command string, errType string, errMsg string) {
	ticket := strings.TrimSpace(req.TicketNo)
	if !logCollectTicketRe.MatchString(ticket) {
		return "", "invalid_ticket", "工单号只允许字母、数字、下划线与连字符（最长 32 位）"
	}

	modules := req.Modules
	if len(modules) == 0 {
		modules = []string{"other"}
	}
	seen := make(map[string]bool, len(modules))
	normalized := make([]string, 0, len(modules))
	for _, module := range modules {
		value := strings.TrimSpace(module)
		if !logCollectModules[value] {
			return "", "invalid_module", fmt.Sprintf("不支持的收集专项：%s", value)
		}
		if seen[value] {
			continue
		}
		seen[value] = true
		normalized = append(normalized, value)
	}

	dateFrom := strings.TrimSpace(req.DateFrom)
	dateTo := strings.TrimSpace(req.DateTo)
	if dateFrom == "" || dateTo == "" {
		return "", "invalid_date", "开始日期与结束日期必填"
	}
	if !logCollectDateRe.MatchString(dateFrom) || !logCollectDateRe.MatchString(dateTo) {
		return "", "invalid_date", "日期格式必须为 YYYY-MM-DD"
	}
	from, err := time.Parse("2006-01-02", dateFrom)
	if err != nil {
		return "", "invalid_date", "开始日期不合法"
	}
	to, err := time.Parse("2006-01-02", dateTo)
	if err != nil {
		return "", "invalid_date", "结束日期不合法"
	}
	if from.After(to) {
		return "", "invalid_date", "开始日期不能晚于结束日期"
	}
	if int(to.Sub(from).Hours()/24)+1 > logCollectMaxRangeDays {
		return "", "invalid_date", fmt.Sprintf("收集范围不能超过 %d 天", logCollectMaxRangeDays)
	}

	ips := strings.TrimSpace(req.IPs)
	if ips == "" {
		ips = "all"
	}
	if ips != "all" {
		for _, ip := range strings.Split(ips, ",") {
			ip = strings.TrimSpace(ip)
			if !logCollectIPv4Re.MatchString(ip) {
				return "", "invalid_ips", fmt.Sprintf("非法主机 IP：%s", ip)
			}
			for _, segment := range strings.Split(ip, ".") {
				value, convErr := strconv.Atoi(segment)
				if convErr != nil || value > 255 {
					return "", "invalid_ips", fmt.Sprintf("非法主机 IP：%s", ip)
				}
			}
		}
	}

	paths := make([]string, 0, len(req.ExtraPaths))
	for _, path := range req.ExtraPaths {
		value := strings.TrimSpace(path)
		if value == "" {
			continue
		}
		if !logCollectPathRe.MatchString(value) || strings.Contains(value, "..") {
			return "", "invalid_path", fmt.Sprintf("非法额外收集路径：%s", value)
		}
		paths = append(paths, value)
	}

	remoteURL := strings.TrimSpace(req.RemoteURL)
	if remoteURL != "" {
		if !strings.HasPrefix(remoteURL, "http://") && !strings.HasPrefix(remoteURL, "https://") {
			return "", "invalid_remote_url", "上传地址必须以 http:// 或 https:// 开头"
		}
		if strings.ContainsAny(remoteURL, " \t\r\n'\"`;|&$") {
			return "", "invalid_remote_url", "上传地址包含非法字符"
		}
	}

	parts := []string{
		"acli", "log_collect", "create",
		"-s", shellQuote(ticket),
		"-m", shellQuote(strings.Join(normalized, ",")),
		"-d", shellQuote(dateFrom + "," + dateTo),
		"-i", shellQuote(ips),
	}
	if len(paths) > 0 {
		parts = append(parts, "-p", shellQuote(strings.Join(paths, ",")))
	}
	if remoteURL != "" {
		parts = append(parts, "-u", shellQuote(remoteURL))
	}
	return strings.Join(parts, " "), "", ""
}

// runCommandPty 在独立 Session 上分配伪终端执行命令并返回合并输出。
// 超时后强制关闭 Session（仅发信号在部分 sshd 上不足以终止容器内进程）。
func (s *SSHSession) runCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error) {
	session, err := s.client.NewSession()
	if err != nil {
		return "", -1, err
	}
	defer session.Close()

	if err := session.RequestPty("xterm-256color", ptyTerminalHeight, ptyTerminalWidth, ptyTerminalModes()); err != nil {
		return "", -1, fmt.Errorf("request pty: %w", err)
	}

	type execOutcome struct {
		output []byte
		err    error
	}
	outcomeCh := make(chan execOutcome, 1)
	go func() {
		output, runErr := session.CombinedOutput(command)
		outcomeCh <- execOutcome{output: output, err: runErr}
	}()

	execCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	select {
	case <-execCtx.Done():
		_ = session.Signal(ssh.SIGKILL)
		_ = session.Close()
		return "", -1, execCtx.Err()
	case outcome := <-outcomeCh:
		exitCode := 0
		if outcome.err != nil {
			var exitErr *ssh.ExitError
			if errors.As(outcome.err, &exitErr) {
				exitCode = exitErr.ExitStatus()
			} else {
				exitCode = -1
			}
		}
		return normalizePtyOutput(string(outcome.output)), exitCode, nil
	}
}

// logCollectProbeResult 是前端确认安装与选择上传方式所需的最小事实集合。
type logCollectProbeResult struct {
	Architecture       string `json:"architecture"`
	AcliInstalled      bool   `json:"acli_installed"`
	AcliVersion        string `json:"acli_version"`
	AcliBuildTime      string `json:"acli_build_time"`
	AvailableMB        int64  `json:"available_mb"`
	LogServerURL       string `json:"log_server_url"`
	LogServerReachable bool   `json:"log_server_reachable"`
	LogServerHTTPCode  int    `json:"log_server_http_code"`
	LogServerLatencyMS int64  `json:"log_server_latency_ms"`
	Note               string `json:"note,omitempty"`
}

// probeLogCollectEnv 采集架构、acli 版本、磁盘余量与日志服务器连通性。
// 所有命令均为只读；任何单项失败都不阻断其余探测。
func (s *SSHSession) probeLogCollectEnv(ctx context.Context, logServerURL string) logCollectProbeResult {
	result := logCollectProbeResult{LogServerURL: logServerURL}

	archOut, _, _ := s.runCommandPty(ctx, "uname -m", 10*time.Second)
	result.Architecture = strings.TrimSpace(archOut)

	versionOut, _, _ := s.runCommandPty(ctx,
		`export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin"; acli --version`,
		10*time.Second)
	result.AcliVersion, result.AcliBuildTime = parseAcliVersion(versionOut)
	result.AcliInstalled = result.AcliVersion != ""

	spaceOut, _, _ := s.runCommandPty(ctx, "df -P -m /sf/data/local | tail -1 | awk '{print $4}'", 10*time.Second)
	result.AvailableMB, _ = strconv.ParseInt(strings.TrimSpace(spaceOut), 10, 64)

	curlProbe := fmt.Sprintf(
		`curl -s -o /dev/null -w '%%{http_code} %%{time_total}' --connect-timeout 5 --max-time 8 --noproxy '*' %s`,
		shellQuote(logServerURL))
	probeOut, probeCode, probeErr := s.runCommandPty(ctx, curlProbe, 15*time.Second)
	if probeErr != nil || probeCode != 0 {
		result.Note = "curl 探测失败：" + firstNonEmpty(errString(probeErr), strings.TrimSpace(probeOut))
		return result
	}
	fields := strings.Fields(probeOut)
	if len(fields) >= 1 {
		result.LogServerHTTPCode, _ = strconv.Atoi(fields[0])
	}
	if len(fields) >= 2 {
		if seconds, parseErr := strconv.ParseFloat(fields[1], 64); parseErr == nil {
			result.LogServerLatencyMS = int64(seconds * 1000)
		}
	}
	// 401/403 也证明网络可达：只有连接失败或 5xx 才视为不可达。
	result.LogServerReachable = result.LogServerHTTPCode >= 200 && result.LogServerHTTPCode < 500
	return result
}

// logCollectProgress 是 acli log_collect progress 的结构化视图。
// ParseOK=false 时前端应原样展示 Raw，且不得据此把任务判为失败。
type logCollectProgress struct {
	Status      string           `json:"status"`
	Percent     int              `json:"percent"`
	Done        int              `json:"done"`
	Total       int              `json:"total"`
	ArchivePath string           `json:"archive_path"`
	TotalTime   string           `json:"total_time,omitempty"`
	Hosts       []logCollectHost `json:"hosts,omitempty"`
	UploadURL   string           `json:"upload_url,omitempty"`
	UploadState string           `json:"upload_state,omitempty"`
	ParseOK     bool             `json:"parse_ok"`
	Raw         string           `json:"raw"`
}

type logCollectHost struct {
	IP      string `json:"ip"`
	Status  string `json:"status"`
	Percent string `json:"percent,omitempty"`
}

// parseLogCollectProgress 解析插件的中文文本输出。
//
// 对抗性审查：插件版本变化可能改文案，因此解析失败必须降级为 Raw 透传，
// 而不是返回零值进度导致前端误判"任务丢失"。
func parseLogCollectProgress(raw string) logCollectProgress {
	result := logCollectProgress{Raw: raw}
	if strings.TrimSpace(raw) == "" {
		return result
	}

	// 任务状态与上传状态共用「状态：」前缀，用「发送地址：」出现位置区分上下文。
	sawUploadBlock := false
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		switch {
		case strings.HasPrefix(line, "发送地址："):
			sawUploadBlock = true
			result.UploadURL = strings.TrimPrefix(line, "发送地址：")
		case strings.HasPrefix(line, "状态："):
			value := strings.TrimPrefix(line, "状态：")
			if sawUploadBlock {
				result.UploadState = value
			} else {
				result.Status = value
			}
		case strings.HasPrefix(line, "压缩包路径"):
			if idx := strings.Index(line, "："); idx >= 0 {
				result.ArchivePath = strings.TrimSpace(line[idx+len("："):])
			}
		case strings.HasPrefix(line, "总耗时："):
			result.TotalTime = strings.TrimPrefix(line, "总耗时：")
		case logCollectProgressRe.MatchString(line):
			match := logCollectProgressRe.FindStringSubmatch(line)
			result.Percent, _ = strconv.Atoi(match[1])
			result.Done, _ = strconv.Atoi(match[2])
			result.Total, _ = strconv.Atoi(match[3])
		case logCollectHostLineRe.MatchString(line):
			match := logCollectHostLineRe.FindStringSubmatch(line)
			result.Hosts = append(result.Hosts, logCollectHost{
				IP: match[1], Status: match[2], Percent: match[3],
			})
		}
	}
	result.ParseOK = result.Status != "" || result.Percent > 0 || result.ArchivePath != ""
	return result
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

// handleLogCollect 处理 log_collect 消息族。
func (b *Bridge) handleLogCollect(ws *websocket.Conn, msg InMessage, cui string) {
	s, key := b.resolveSession(msg)
	if s == nil && msg.CaseID != "" {
		s = b.get(sessionKey(msg.CaseID, ""))
	}
	if s == nil {
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: msg.LogAction,
			Status: "error", ErrorType: "session_missing",
			Message: "SSH 会话不存在（需先 ssh_connect）", CustomUI: cui,
		})
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), logCollectCancelTimeout)
	defer cancel()

	action := strings.TrimSpace(msg.LogAction)

	switch action {
	case "probe":
		logServer := strings.TrimSpace(msg.RemoteURL)
		if logServer == "" {
			logServer = logCollectDefaultLogServer
		}
		probe := s.probeLogCollectEnv(ctx, logServer)
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "ok", LogData: probe, CustomUI: cui,
		})
		blog("INFO", "log_collect.probe", "日志收集环境探测完成", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": probe.Architecture, "acli_installed": probe.AcliInstalled,
			"acli_version": probe.AcliVersion, "available_mb": probe.AvailableMB,
			"log_server_reachable": probe.LogServerReachable, "http_code": probe.LogServerHTTPCode,
			"key": key,
		})

	case "create":
		command, errType, errMsg := buildLogCollectCreateCommand(msg)
		if errType != "" {
			sendMsg(ws, OutMessage{
				Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
				Status: "error", ErrorType: errType, Message: errMsg, CustomUI: cui,
			})
			blog("ERROR", "log_collect.rejected", "日志收集参数校验失败", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"error_type": errType, "reason": errMsg,
			})
			return
		}
		output, exitCode, err := s.runCommandPty(ctx, command, logCollectCreateTimeout)
		status := "ok"
		if err != nil || exitCode != 0 {
			status = "error"
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, Raw: output,
			Message: firstNonEmpty(errString(err), strings.TrimSpace(output)),
			Detail:  command, CustomUI: cui,
		})
		blog("INFO", "log_collect.create", "日志收集任务下发", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"ticket_no": msg.TicketNo, "exit_code": exitCode, "error": errString(err),
			"command": redactCommand(command),
		})

	case "progress":
		output, exitCode, err := s.runCommandPty(ctx, "acli log_collect progress", logCollectProgressTimeout)
		progress := parseLogCollectProgress(output)
		status := "ok"
		if err != nil || exitCode != 0 {
			status = "error"
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, LogData: progress, Raw: output,
			Message: errString(err), CustomUI: cui,
		})
		blog("INFO", "log_collect.progress", "日志收集进度查询", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"parse_ok": progress.ParseOK, "percent": progress.Percent,
			"status_text": progress.Status, "exit_code": exitCode,
		})

	case "cancel":
		output, exitCode, err := s.runCommandPty(ctx, "acli log_collect cancel", logCollectCancelTimeout)
		status := "ok"
		if err != nil || exitCode != 0 {
			status = "error"
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, Raw: output,
			Message: firstNonEmpty(errString(err), strings.TrimSpace(output)), CustomUI: cui,
		})
		blog("WARN", "log_collect.cancel", "日志收集任务取消", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"exit_code": exitCode, "error": errString(err),
		})

	default:
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "error", ErrorType: "invalid_action",
			Message: "log_action 仅支持 probe / create / progress / cancel", CustomUI: cui,
		})
	}
}
