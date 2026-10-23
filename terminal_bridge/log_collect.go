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
	"encoding/json"
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

// logCollectModuleOption 是前端下拉的一项：value 为 -m 的取值，label 为人工可读文案。
type logCollectModuleOption struct {
	Value string `json:"value"`
	Label string `json:"label"`
}

// logCollectHostOption 是 acli 上报的节点清单项。
// 第一性原理：目标主机必须来自被采集集群的事实，而不是让用户凭记忆手填 IP。
type logCollectHostOption struct {
	IP     string `json:"ip"`
	Name   string `json:"name,omitempty"`
	Master bool   `json:"master,omitempty"`
}

// logCollectModuleLabels 是平台内置场景目录，文案取自 acli 1.0.1 帮助原文。
// 对抗性审查：help 解析可能因插件升级改版而失败，内置目录保证下拉永远有可选项，
// 而不是把「探测失败」变成「无法收集」。
var logCollectModuleLabels = map[string]string{
	"vm_suspend":   "虚拟机挂起专项",
	"vm_start":     "虚拟机开机失败专项",
	"host_offline": "主机离线专项",
	"network":      "网络专项",
	"storage":      "虚拟存储专项",
	"slowdisk":     "卡慢盘专项",
	"performance":  "性能专项",
	"core":         "core 文件专项",
	"system":       "中台专项",
	"other":        "默认收集专项（不收集上述场景日志）",
}

// logCollectModuleCatalogOrder 固定内置目录顺序，避免 map 迭代导致下拉顺序抖动。
var logCollectModuleCatalogOrder = []string{
	"vm_suspend", "vm_start", "host_offline", "network", "storage",
	"slowdisk", "performance", "core", "system", "other",
}

// logCollectAcliPathEnv 是 acli 的固定调用前缀：容器 PATH 不含 /sf/bin，
// 且插件位于 /sf/data/local/acli，缺一不可。
const logCollectAcliPathEnv = `export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin";`

func builtinLogCollectModules() []logCollectModuleOption {
	options := make([]logCollectModuleOption, 0, len(logCollectModuleCatalogOrder))
	for _, value := range logCollectModuleCatalogOrder {
		options = append(options, logCollectModuleOption{Value: value, Label: logCollectModuleLabels[value]})
	}
	return options
}

var (
	logCollectTicketRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,32}$`)
	logCollectDateRe   = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)
	logCollectIPv4Re   = regexp.MustCompile(`^(\d{1,3}\.){3}\d{1,3}$`)
	// 额外收集路径只允许绝对路径，并禁止 shell 元字符与空白（插件会直接拼命令）。
	logCollectPathRe     = regexp.MustCompile(`^/[A-Za-z0-9_./-]+$`)
	logCollectProgressRe = regexp.MustCompile(`^总进度：(\d+)% - (\d+)/(\d+)`)
	logCollectHostLineRe = regexp.MustCompile(`^(\d{1,3}(?:\.\d{1,3}){3})\s+(收集完成|收集中|收集失败)\s+(\d+%|-)`)
	// 实机 help 把枚举值按两列排版（同一行出现两个「名称（标签）」），
	// 因此必须全量扫描而不是逐行只取首个匹配。
	logCollectModuleOptionRe = regexp.MustCompile(`([a-z][a-z_]+)（([^）]{1,40})）`)
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
	// 准备阶段的事实来源：场景目录与节点清单都必须由被采集集群给出，不能硬编码在前端。
	Modules       []logCollectModuleOption `json:"modules"`
	Hosts         []logCollectHostOption   `json:"hosts"`
	CatalogSource string                   `json:"catalog_source"`
	CatalogNote   string                   `json:"catalog_note,omitempty"`
	Note          string                   `json:"note,omitempty"`
}

// probeLogCollectEnv 采集架构、acli 版本、磁盘余量与日志服务器连通性。
// 所有命令均为只读；任何单项失败都不阻断其余探测。
func (s *SSHSession) probeLogCollectEnv(ctx context.Context, logServerURL string) logCollectProbeResult {
	result := logCollectProbeResult{LogServerURL: logServerURL}

	archOut, _, _ := s.runCommandPty(ctx, "uname -m", 10*time.Second)
	result.Architecture = strings.TrimSpace(archOut)

	versionOut, _, _ := s.runCommandPty(ctx, logCollectAcliPathEnv+" acli --version", 10*time.Second)
	result.AcliVersion, result.AcliBuildTime = parseAcliVersion(versionOut)
	result.AcliInstalled = result.AcliVersion != ""

	// 场景目录与节点清单必须在 curl 之前采集：日志服务器不可达（P3 路径）是常态，
	// 早期 return 不能把它们一起丢掉。
	s.probeLogCollectCatalog(ctx, &result)

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

// probeLogCollectCatalog 采集「场景目录」与「节点清单」，供前端准备阶段渲染。
//
// 第一性原理：可选场景与可采主机是集群的事实，不是前端常量；只能从 acli 读。
// 对抗性审查：这两项任何失败都不得阻断收集，只做降级——目录回退内置文案，
// 主机清单留空并提示手填，避免"探测不到就完全不能用"。
func (s *SSHSession) probeLogCollectCatalog(ctx context.Context, result *logCollectProbeResult) {
	if !result.AcliInstalled {
		result.Modules = builtinLogCollectModules()
		result.CatalogSource = "platform_default"
		result.CatalogNote = "acli 未安装，使用平台内置场景目录"
		return
	}

	// 实机验证：`acli log_collect create --help` 会以 exit code 1 结束
	// （插件把 --help 判为未定义参数），但正文已完整打印，因此只认输出内容、不认退出码。
	helpOut, _, _ := s.runCommandPty(ctx, logCollectAcliPathEnv+" acli log_collect create --help", 8*time.Second)
	if modules := parseLogCollectModuleCatalog(helpOut); len(modules) > 0 {
		result.Modules = modules
		result.CatalogSource = "acli_help"
	} else {
		result.Modules = builtinLogCollectModules()
		result.CatalogSource = "platform_default"
		result.CatalogNote = "acli 帮助解析失败，使用平台内置场景目录"
	}

	nodeOut, _, _ := s.runCommandPty(ctx, logCollectAcliPathEnv+" acli --formatter json platform node list", 8*time.Second)
	result.Hosts = parseLogCollectNodeList(nodeOut)
	if len(result.Hosts) == 0 {
		result.CatalogNote = appendProbeNote(result.CatalogNote, "节点清单获取失败，可手动填写 IP")
	}
}

// appendProbeNote 拼接提示文案，避免用空串拼出多余分隔符。
func appendProbeNote(current, extra string) string {
	if strings.TrimSpace(current) == "" {
		return extra
	}
	return current + "；" + extra
}

// parseLogCollectModuleCatalog 从 help 文本提取场景枚举。
//
// 对抗性审查：实机 help 把枚举值按两列排版（同一行有两个「名称（标签）」），
// 且正文里还夹着示例命令，因此只解析「枚举值」到「说明」之间的段落并全量扫描。
func parseLogCollectModuleCatalog(raw string) []logCollectModuleOption {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	block := raw
	idx := strings.Index(block, "枚举值")
	if idx < 0 {
		// 文案改版时不做猜测，交给调用方回退内置目录。
		return nil
	}
	block = block[idx:]
	if end := strings.Index(block, "说明"); end >= 0 {
		block = block[:end]
	}

	seen := make(map[string]bool, len(logCollectModules))
	options := make([]logCollectModuleOption, 0, len(logCollectModules))
	for _, match := range logCollectModuleOptionRe.FindAllStringSubmatch(block, -1) {
		value := match[1]
		// 白名单之外的枚举不展示：否则前端可选、执行时却被 create 拒绝，体验更差。
		if !logCollectModules[value] || seen[value] {
			continue
		}
		seen[value] = true
		options = append(options, logCollectModuleOption{
			Value: value,
			Label: strings.TrimSpace(match[2]),
		})
	}
	return options
}

// parseLogCollectNodeList 解析 `acli --formatter json platform node list` 的输出。
// PTY 可能混入提示符或告警，因此只截取最外层 JSON 对象；失败返回 nil。
func parseLogCollectNodeList(raw string) []logCollectHostOption {
	start := strings.Index(raw, "{")
	end := strings.LastIndex(raw, "}")
	if start < 0 || end <= start {
		return nil
	}
	var payload struct {
		Data []struct {
			IP     string `json:"ip"`
			Name   string `json:"name"`
			Master int    `json:"master"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw[start:end+1]), &payload); err != nil {
		return nil
	}

	seen := make(map[string]bool, len(payload.Data))
	hosts := make([]logCollectHostOption, 0, len(payload.Data))
	for _, node := range payload.Data {
		ip := strings.TrimSpace(node.IP)
		if !logCollectIPv4Re.MatchString(ip) || seen[ip] {
			continue
		}
		seen[ip] = true
		hosts = append(hosts, logCollectHostOption{
			IP:     ip,
			Name:   strings.TrimSpace(node.Name),
			Master: node.Master == 1,
		})
	}
	return hosts
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
	// 链路 ID 先算：会话缺失这类失败同样要能按链路检索与聚合。
	traceID := strings.TrimSpace(msg.TraceID)
	if traceID == "" {
		traceID = "lc-" + firstNonEmpty(strings.TrimSpace(msg.ExecID), "unknown")
	}
	msg.TraceID = traceID

	s, key := b.resolveSession(msg)
	if s == nil && msg.CaseID != "" {
		s = b.get(sessionKey(msg.CaseID, ""))
	}
	// 回退：会话属于「操作者 + 节点」，不随工单变化。
	// 精确键失配（例如会话是在 SSH 终端/旧工单下建立的）时按节点 IP 复用，
	// 避免"界面显示已连接、探测却报 LC-ENV-006"的自相矛盾。
	if s == nil {
		if fallback, fallbackKey := b.sessionByNodeIP(msg.NodeIP); fallback != nil {
			s, key = fallback, fallbackKey
			blog("INFO", "log_collect.session_fallback", "日志收集复用同节点其他工单的 SSH 会话", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"log_action": msg.LogAction, "session_key": fallbackKey, "match": "node_ip_suffix",
			})
		}
	}
	// 再回退：会话键可能压根不含 IP（ssh_connect 历史上不带 node_ip，键只剩 caseID），
	// 此时按会话实际连到的主机地址匹配，"终端里能跑 acli、日志收集却说没会话"才能消除。
	if s == nil {
		if fallback, fallbackKey := b.sessionByAddressHost(msg.NodeIP); fallback != nil {
			s, key = fallback, fallbackKey
			blog("INFO", "log_collect.session_fallback", "日志收集按会话目标地址复用 SSH 会话", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"log_action": msg.LogAction, "session_key": fallbackKey, "match": "address_host",
			})
		}
	}
	if s == nil {
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: msg.LogAction,
			Status: "error", ErrorType: "session_missing", ErrorCode: ErrEnvSessionMissing,
			Message: "SSH 会话不存在（需先 ssh_connect）", TraceID: traceID, CustomUI: cui,
		})
		blog("ERROR", "log_collect.session_missing", "日志收集动作缺少 SSH 会话", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"log_action": msg.LogAction, "task_id": msg.ExecID,
		})
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), logCollectCancelTimeout)
	defer cancel()

	action := strings.TrimSpace(msg.LogAction)
	startedAt := time.Now()
	outcome := "error"
	// 统一收尾日志：任何分支（含提前 return）都会留下耗时与结果，便于远端定位。
	defer func() {
		blog("INFO", "log_collect.done", "日志收集动作结束", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"log_action": action, "task_id": msg.ExecID, "outcome": outcome,
			"duration_ms": time.Since(startedAt).Milliseconds(),
		})
	}()
	blog("INFO", "log_collect.start", "日志收集动作开始", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
		"log_action": action, "task_id": msg.ExecID, "ticket_no": msg.TicketNo,
		"modules": msg.Modules, "date_from": msg.DateFrom, "date_to": msg.DateTo,
		"ips": msg.IPs, "extra_paths_count": len(msg.ExtraPaths),
		"remote_url_set": strings.TrimSpace(msg.RemoteURL) != "",
	})

	switch action {
	case "probe":
		logServer := strings.TrimSpace(msg.RemoteURL)
		if logServer == "" {
			logServer = logCollectDefaultLogServer
		}
		probe := s.probeLogCollectEnv(ctx, logServer)
		outcome = "ok"
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "ok", LogData: probe, TraceID: traceID, CustomUI: cui,
		})
		blog("INFO", "log_collect.probe", "日志收集环境探测完成", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"architecture": probe.Architecture, "acli_installed": probe.AcliInstalled,
			"acli_version": probe.AcliVersion, "acli_build_time": probe.AcliBuildTime,
			"available_mb":   probe.AvailableMB,
			"log_server_url": probe.LogServerURL, "log_server_reachable": probe.LogServerReachable,
			"http_code": probe.LogServerHTTPCode, "latency_ms": probe.LogServerLatencyMS,
			"catalog_source": probe.CatalogSource, "modules_count": len(probe.Modules),
			"hosts_count": len(probe.Hosts), "catalog_note": probe.CatalogNote,
			"note": probe.Note, "key": key,
		})

	case "create":
		command, errType, errMsg := buildLogCollectCreateCommand(msg)
		if errType != "" {
			sendMsg(ws, OutMessage{
				Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
				Status: "error", ErrorType: errType, ErrorCode: validationErrorCode(errType),
				Message: errMsg, TraceID: traceID, CustomUI: cui,
			})
			blog("ERROR", "log_collect.rejected", "日志收集参数校验失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"error_type": errType, "reason": errMsg, "ticket_no": msg.TicketNo,
			})
			return
		}
		output, exitCode, err := s.runCommandPty(ctx, command, logCollectCreateTimeout)
		status := "ok"
		code := ""
		if err != nil || exitCode != 0 {
			status = "error"
			code = execErrorCode(err, exitCode)
		} else {
			outcome = "ok"
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, Raw: output, ErrorCode: code,
			Message: firstNonEmpty(errString(err), strings.TrimSpace(output)),
			Detail:  command, TraceID: traceID, CustomUI: cui,
		})
		blog("INFO", "log_collect.create", "日志收集任务下发", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"ticket_no": msg.TicketNo, "exit_code": exitCode, "error": errString(err),
			"command": redactCommand(command), "output_preview": truncateString(output, 512),
		})

	case "progress":
		output, exitCode, err := s.runCommandPty(ctx, "acli log_collect progress", logCollectProgressTimeout)
		progress := parseLogCollectProgress(output)
		status := "ok"
		code := ""
		if err != nil || exitCode != 0 {
			status = "error"
			code = execErrorCode(err, exitCode)
		} else {
			outcome = "ok"
			if !progress.ParseOK {
				// 解析降级不是失败：单独出码，前端展示原文，任务状态仍为进行中。
				code = ErrPlugProgressDegr
			}
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, LogData: progress, Raw: output, ErrorCode: code,
			Message: errString(err), TraceID: traceID, CustomUI: cui,
		})
		blog("INFO", "log_collect.progress", "日志收集进度查询", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"parse_ok": progress.ParseOK, "percent": progress.Percent,
			"done": progress.Done, "total": progress.Total,
			"status_text": progress.Status, "archive_path": progress.ArchivePath,
			"upload_url": progress.UploadURL, "upload_state": progress.UploadState,
			"hosts_count": len(progress.Hosts), "exit_code": exitCode,
			"error": errString(err), "raw_len": len(output),
		})

	case "stat":
		// P3 编排需要先知道包大小才能 init 分片会话；只读单命令，走非 PTY 通道。
		path, pathErr := normalizeArchivePath(msg.TransferSourcePath)
		if pathErr != nil {
			sendMsg(ws, OutMessage{
				Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
				Status: "error", ErrorType: "invalid_path", ErrorCode: ErrTransferArchiveMissing,
				Message: pathErr.Error(), TraceID: traceID, CustomUI: cui,
			})
			return
		}
		output, exitCode, cmdErr := s.runQuickCommand(ctx, fmt.Sprintf("stat -c %%s %s", shellQuote(path)))
		size, parseErr := strconv.ParseInt(strings.TrimSpace(output), 10, 64)
		status, code := "ok", ""
		if cmdErr != nil || exitCode != 0 || parseErr != nil {
			status, code = "error", ErrTransferArchiveMissing
		} else {
			outcome = "ok"
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, ErrorCode: code,
			LogData: map[string]any{"path": path, "size": size},
			Message: firstNonEmpty(errString(cmdErr), errString(parseErr)), TraceID: traceID, CustomUI: cui,
		})
		blog("INFO", "log_collect.stat", "日志包大小探测", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"path": path, "size": size, "exit_code": exitCode, "error": errString(cmdErr),
		})

	case "transfer":
		// 传输可能持续数十分钟，不沿用 180s 的动作超时，单独给 2 小时预算。
		transferCtx, transferCancel := context.WithTimeout(context.Background(), logCollectTransferTimeout)
		defer transferCancel()
		if err := b.handleLogCollectTransfer(transferCtx, ws, s, msg, cui, traceID); err != nil {
			blog("ERROR", "log_collect.transfer", "日志包交付失败", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
				"task_id": msg.ExecID, "error": err.Error(),
			})
			return
		}
		outcome = "ok"

	case "cancel":
		output, exitCode, err := s.runCommandPty(ctx, "acli log_collect cancel", logCollectCancelTimeout)
		status := "ok"
		code := ""
		if err != nil || exitCode != 0 {
			status = "error"
			code = ErrPlugCancelFailed
		} else {
			outcome = "ok"
		}
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: status, ExitCode: exitCode, Raw: output, ErrorCode: code,
			Message: firstNonEmpty(errString(err), strings.TrimSpace(output)),
			TraceID: traceID, CustomUI: cui,
		})
		blog("WARN", "log_collect.cancel", "日志收集任务取消", traceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
			"exit_code": exitCode, "error": errString(err),
			"output_preview": truncateString(output, 512),
		})

	default:
		sendMsg(ws, OutMessage{
			Type: "log_collect_result", CaseID: msg.CaseID, LogAction: action,
			Status: "error", ErrorType: "invalid_action", ErrorCode: ErrPlugInvalidAction,
			Message: "log_action 仅支持 probe / create / progress / cancel / stat / transfer",
			TraceID: traceID, CustomUI: cui,
		})
	}
}
