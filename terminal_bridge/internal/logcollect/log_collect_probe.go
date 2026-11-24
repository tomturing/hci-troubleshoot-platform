package logcollect

// 日志收集的**命令构建与节点探测**：create 命令行、环境/专项探测、progress 与
// help 输出解析、归档存在性与失败归因。
//
// 与 log_collect.go 的分工：那边只管消息族编排（HandleLogCollect）与状态机推进，
// 这里只管"怎么问节点、怎么读节点的话"。两者都不含协议态。

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"terminal_bridge/internal/shared/aclibin"
	"terminal_bridge/internal/shared/acliout"
	"terminal_bridge/internal/shared/cluster"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/sshx"
	"time"
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
//
// 阶段 2 A3：结构本体已归 shared/models.NodeOption —— 它同时是 WS 出站契约与
// 集群拓扑解析结果，由 acli 与 logcollect 共用。此处保留别名，使采集编排内部
// 与既有测试无需改动。
type logCollectHostOption = models.NodeOption

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

// logCollectAcliInvoke 把「acli 之后的参数串」包成可直接下发的自解析命令。
//
// 阶段 2 A3：PATH 前缀、绝对路径解析与输出协议已下沉到 shared/aclibin——
// 它是 logcollect 与 acli 两个域共用的"节点上跑哪份 acli"事实层，
// 放在任一侧都会让另一侧反向依赖（或各写一份，重现"显示与执行不一致"的老缺陷）。
func logCollectAcliInvoke(args string) string {
	return aclibin.Invoke(args)
}

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
	logCollectPathRe = regexp.MustCompile(`^/[A-Za-z0-9_./-]+$`)
	// 进度行按「标签 + 百分比 + 分数」的形态匹配，中英文共用一条：
	// 中文「总进度：63% - 2/3（已收集完成/收集总数）」、英文
	// 「Overall progress: 100% - 5/5(Collected/Total collected)」。
	// 终端颜色/样式转义的清洗已收口到 remote.StripANSI（唯一实现）。
	// acli 失败时会输出 `\x1b[31mFATA\x1b[0m...`，不清洗会让状态行前缀带转义、
	// 解析失配，前端也会把乱码直接展示给用户。
	// 只用于剥掉 acli 日志行前面的时间戳，避免把日志头当成失败原因。
	// 兼容 `2026/11/12 10:00:00`、`2026-11-12T10:00:00.123Z`、`2026-11-12 10:00:00+08:00`。
	logCollectTimestampRe = regexp.MustCompile(`^\d{4}[/-]\d{2}[/-]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?`)
	// 节点行只锚定「行首 IP + 中间状态列 + 行尾百分比/占位横线」三段的形态，
	// 中间列不写死文案：中文「收集完成」与英文「Collection successful」都是两个词的
	// 组合，任何按词枚举的写法都会在下一次文案变化时失效，交给
	// classifyLogCollectHostStatusWord 归一化后再判定这一行是否是节点明细。
	// 实机 help 把枚举值按两列排版（同一行出现两个「名称（标签）」），
	// 因此必须全量扫描而不是逐行只取首个匹配。括号兼容全角与半角，
	// 并允许标签前有空格：中文是 `host_offline（主机离线）`，
	// 英文是 `host_offline (Host offline)`。只吃空格/制表符，不跨行。
	// 标签上限 120：25 节点英文 help 的 other 说明长达 72 字符
	// （Default Collection Issue, does not collect logs for the above n scenarios），
	// 旧上限 40 会让 slowdisk/other 整条匹配失败，被误报为「节点可能不支持」。
	logCollectModuleOptionRe = regexp.MustCompile(`([a-z][a-z_]+)[ \t]*[（(][ \t]*([^）)]{1,120})[）)]`)
)

// buildLogCollectCreateCommand 校验参数并返回 acli 之后的参数串（不含 acli 本体）。
// 调用方必须经 logCollectAcliInvoke 包成自解析命令，禁止裸拼 `acli`：
// 非交互 SSH 会话的 PATH 不含 /sf/bin|/sf/sbin，裸命令必然 command not found。
// 返回的 errType 非空表示拒绝执行，调用方应把原因回传前端。
func buildLogCollectCreateCommand(req models.InMessage) (command string, errType string, errMsg string) {
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

	// 票据只允许出现在 -L 的值里。字符集收窄是硬要求：bridge 拼的是 shell
	// 命令，而 acli 自己不防注入，任何来自平台的字符串都必须先过白名单。
	uploadTicket := strings.TrimSpace(req.UploadTicket)
	if uploadTicket != "" {
		if !logCollectUploadTicketRe.MatchString(uploadTicket) {
			return "", "invalid_upload_ticket", "P1 直传票据格式非法"
		}
		if remoteURL == "" {
			return "", "invalid_upload_ticket", "P1 直传票据必须与上传地址同时出现"
		}
	}

	parts := []string{
		"log_collect", "create",
		"-s", remote.ShellQuote(ticket),
		"-m", remote.ShellQuote(strings.Join(normalized, ",")),
		"-d", remote.ShellQuote(dateFrom + "," + dateTo),
		"-i", remote.ShellQuote(ips),
	}
	if len(paths) > 0 {
		parts = append(parts, "-p", remote.ShellQuote(strings.Join(paths, ",")))
	}
	if remoteURL != "" {
		parts = append(parts, "-u", remote.ShellQuote(remoteURL))
		if uploadTicket != "" {
			// 用 -L（URL 特定请求头）而不是 -H（全局请求头）：
			// -H 会把凭据带给 acli 访问的每一个地址，暴露面必须按 URL 限定到
			// 第 0 个上传地址（index 从 0 开始）。
			// 用 -H（全局请求头）而不是 -L（URL 特定请求头）。
			//
			// 现场 Q2026110601054 实测：命令里带 -L '0:Authorization:Bearer …' 时，
			// acli 正常接受参数并下发任务，但 5888 收到的 /init_upload 里没有任何
			// Authorization 头（日志只有"鉴权失败"、没有我们新增的 [P1] 与 reason）。
			// 也就是说 1.0.0 解析了 -L 却没把它带到上传请求上。
			//
			// 这里换成全局头做隔离验证：若 -H 能发出去，说明是 -L 的 index 语义
			// （引用"已配置的目标地址"而非命令行 -u）导致；若 -H 也发不出去，
			// 说明该版本的自定义头参数对上传链路是死参数，P1 带凭据不可行。
			parts = append(parts, "-H", remote.ShellQuote(logCollectUploadHeader(uploadTicket)))
		}
	}
	return strings.Join(parts, " "), "", ""
}

// logCollectUploadTicketRe 限定票据字符集：只允许 base64url 与点分隔符。
// 平台签发的形状是 p1.v1.<payload>.<hex>，长度 100~600 之间。
var logCollectUploadTicketRe = regexp.MustCompile(`^[A-Za-z0-9._-]{16,600}$`)

// logCollectUploadHeader 构造 acli `-H` 的取值：Header-Name: value。
func logCollectUploadHeader(uploadTicket string) string {
	return "Authorization: Bearer " + uploadTicket
}

// redactUploadTicket 把完整命令里的票据替换掉，供日志与前端展示。
//
// 为什么不能只靠通用脱敏：bridge 的 sensitiveTextPatterns 靠
// password/token/secret 等关键词匹配，而 `Authorization: Bearer x` 里
// 一个关键词都没有，会被原样写进 command 字段与 detail。
func redactUploadTicket(command, uploadTicket string) string {
	uploadTicket = strings.TrimSpace(uploadTicket)
	if uploadTicket == "" {
		return command
	}
	return strings.ReplaceAll(command, uploadTicket, "[REDACTED]")
}

// logCollectProbeResult 是前端确认安装与选择上传方式所需的最小事实集合。
type logCollectProbeResult struct {
	Architecture  string `json:"architecture"`
	AcliInstalled bool   `json:"acli_installed"`
	AcliVersion   string `json:"acli_version"`
	AcliBuildTime string `json:"acli_build_time"`
	// AcliBinary 是本次探测真正命中的 acli 路径：多份 acli 共存时，仅凭版本号无法判断
	// "面板显示的值"与"运维手敲的值"为何不同，路径是唯一可自证的事实。
	AcliBinary         string `json:"acli_binary,omitempty"`
	AvailableMB        int64  `json:"available_mb"`
	LogServerURL       string `json:"log_server_url"`
	LogServerReachable bool   `json:"log_server_reachable"`
	LogServerHTTPCode  int    `json:"log_server_http_code"`
	LogServerLatencyMS int64  `json:"log_server_latency_ms"`
	// AcliSupportsUploadHeader 表示本节点的 acli 是否支持 `-L|--url-header`。
	//
	// 这是 P1 能否带凭据上传的硬前提：不支持就只能走 P2（平台代传带 Bearer）。
	// 判据直接取 `acli log_collect create --help` 的原文——版本号不可靠，
	// 同一版本号的不同构建实测参数集并不一致。
	//
	// 注意它**不**等于"P1 可用"：还要平台已配置票据密钥、且网络可达。
	// bridge 不知道平台密钥状态，因此这里只回答"节点侧能力"。
	AcliSupportsUploadHeader bool `json:"acli_supports_upload_header"`
	// 准备阶段的事实来源：场景目录与节点清单都必须由被采集集群给出，不能硬编码在前端。
	Modules       []logCollectModuleOption `json:"modules"`
	Hosts         []logCollectHostOption   `json:"hosts"`
	CatalogSource string                   `json:"catalog_source"`
	CatalogNote   string                   `json:"catalog_note,omitempty"`
	// NodeModules 是节点 help 真实列出的白名单专项（不补全）。Modules 恒为白名单
	// 全集以保证可选性，两者之差即"节点可能不支持"的专项，前端据此渲染非阻塞
	// 版本警示（Q2026111700614：降级到 1.0.0 后 core 不在枚举内，create 必失败）。
	NodeModules []string `json:"node_modules,omitempty"`
	Note        string   `json:"note,omitempty"`
}

// ── 日志服务器连通性：有界重试 ─────────────────────────────────────────────
//
// 第一性原理：一次 curl 没收到任何响应，只证明「这几秒里没通」，不证明「这里不通」。
// 而这条结论在下游是硬性的——面板据此把 P1 置灰、强制改走 P2（平台留副本、多一跳中转）。
// 用一个 5 秒的采样去否掉一条交付路径，代价要么是让用户白等一次注定失败的直传，
// 要么是白绕一跳中转，所以「不可达」必须由多次一致观测才成立。
//
// 对抗性审查（哪些情况不重试、为什么次数必须小）：
//   - 收到 HTTP 应答（含 5xx）说明网络通、是服务端在表态，重试只拖长面板而不改变结论；
//   - 内网 HCI 本来出不了公网，重试仍会全部耗到超时——所以封顶 3 次，
//     且只在传输层失败（没拿到 curl 输出）时发生，不把常见现场拖成二三十秒白等。
const logServerProbeMaxAttempts = 3

// logServerProbeRetryDelays 是第 2、3 次尝试前的等待：
// 抖动消失以百毫秒计，但也要给 TCP 重传与临时 ACL 一点时间，1s/2s 足够。
var logServerProbeRetryDelays = []time.Duration{1 * time.Second, 2 * time.Second}

// logServerProbeSample 是一次 curl 探测的取样结果。
type logServerProbeSample struct {
	// HTTPCode 为 0 表示一个响应字节都没收到。
	HTTPCode  int
	LatencyMS int64
	// TransportFailed 表示这次没拿到 curl 的 -w 输出（超时 / 拒绝 / 解析失败 / PTY 异常），
	// 只有这类失败存在「这次没成」与「这里不通」的歧义。
	TransportFailed bool
	// Detail 是这次失败的原文佐证（curl 输出或进程错误）。
	Detail string
}

// confirmLogServerReachability 用有界重试把「一次没通」收敛成确定结论。
//
// sample / sleep 可注入：重试语义必须能在不碰 SSH 的单测里被验证。
// 第二个返回值是要写进探测快照 note 的说明——两种结局都必须留痕，否则「网络抖过」
// 与「确实不通」在探测历史里长得一模一样。
func confirmLogServerReachability(
	sample func() logServerProbeSample,
	delays []time.Duration,
	sleep func(time.Duration) bool,
) (logServerProbeSample, string) {
	first := sample()
	if !first.TransportFailed {
		return first, ""
	}
	last := first
	attempts := 1
	for _, delay := range delays {
		if attempts >= logServerProbeMaxAttempts {
			break
		}
		if sleep != nil && !sleep(delay) {
			break
		}
		attempts++
		last = sample()
		if !last.TransportFailed {
			return last, fmt.Sprintf("日志服务器第 1 次探测无响应（%s），第 %d 次收到响应",
				sshx.TruncateString(first.Detail, 200), attempts)
		}
	}
	return last, fmt.Sprintf("curl 探测失败（连续 %d 次）：%s",
		attempts, sshx.TruncateString(firstNonEmpty(last.Detail, first.Detail), 200))
}

// probeLogCollectEnv 采集架构、acli 版本、磁盘余量与日志服务器连通性。
// 所有命令均为只读；任何单项失败都不阻断其余探测。
func probeLogCollectEnv(s *sshx.SSHSession, ctx context.Context, logServerURL string) logCollectProbeResult {
	result := logCollectProbeResult{LogServerURL: logServerURL}

	archOut, _, _ := s.RunCommandPty(ctx, "uname -m", 10*time.Second)
	result.Architecture = strings.TrimSpace(archOut)

	versionOut, _, _ := s.RunCommandPty(ctx, aclibin.VersionCommand(), 10*time.Second)
	// 版本解析唯一实现在 remote.ParseVersionOutput（acli.ParseAcliVersion 为其薄包装）。
	vinfo := remote.ParseVersionOutput(versionOut)
	result.AcliVersion, result.AcliBuildTime = vinfo.Version, vinfo.BuildTime
	result.AcliBinary = aclibin.ParseBinary(versionOut)
	result.AcliInstalled = result.AcliVersion != ""

	// 场景目录与节点清单必须在 curl 之前采集：日志服务器不可达（P3 路径）是常态，
	// 早期 return 不能把它们一起丢掉。
	probeLogCollectCatalog(s, ctx, &result)

	spaceOut, _, _ := s.RunCommandPty(ctx, "df -P -m /sf/data/local | tail -1 | awk '{print $4}'", 10*time.Second)
	result.AvailableMB, _ = strconv.ParseInt(strings.TrimSpace(spaceOut), 10, 64)

	curlProbe := fmt.Sprintf(
		`curl -s -o /dev/null -w '%%{http_code} %%{time_total}' --connect-timeout 5 --max-time 8 --noproxy '*' %s`,
		remote.ShellQuote(logServerURL))
	sample := func() logServerProbeSample {
		probeOut, probeCode, probeErr := s.RunCommandPty(ctx, curlProbe, 15*time.Second)
		if probeErr != nil || probeCode != 0 {
			return logServerProbeSample{
				TransportFailed: true,
				Detail:          firstNonEmpty(errString(probeErr), strings.TrimSpace(probeOut)),
			}
		}
		one := logServerProbeSample{}
		fields := strings.Fields(probeOut)
		if len(fields) >= 1 {
			one.HTTPCode, _ = strconv.Atoi(fields[0])
		}
		if len(fields) >= 2 {
			if seconds, parseErr := strconv.ParseFloat(fields[1], 64); parseErr == nil {
				one.LatencyMS = int64(seconds * 1000)
			}
		}
		return one
	}
	// ctx 结束（面板关闭 / 动作超时）后不再空等：直接按已有观测收尾，不制造一个假的「在重试」。
	settled, note := confirmLogServerReachability(sample, logServerProbeRetryDelays, func(delay time.Duration) bool {
		select {
		case <-ctx.Done():
			return false
		case <-time.After(delay):
			return true
		}
	})
	result.LogServerHTTPCode = settled.HTTPCode
	result.LogServerLatencyMS = settled.LatencyMS
	result.Note = note
	// 401/403 也证明网络可达：只有连接失败或 5xx 才视为不可达。
	result.LogServerReachable = settled.HTTPCode >= 200 && settled.HTTPCode < 500
	return result
}

// probeLogCollectCatalog 采集「场景目录」与「节点清单」，供前端准备阶段渲染。
//
// 第一性原理：可选场景与可采主机是集群的事实，不是前端常量；只能从 acli 读。
// 对抗性审查：这两项任何失败都不得阻断收集，只做降级——目录回退内置文案，
// 主机清单留空并提示手填，避免"探测不到就完全不能用"。
func probeLogCollectCatalog(s *sshx.SSHSession, ctx context.Context, result *logCollectProbeResult) {
	if !result.AcliInstalled {
		result.Modules = builtinLogCollectModules()
		result.CatalogSource = "platform_default"
		result.CatalogNote = "acli 未安装，使用平台内置场景目录"
		return
	}

	// 实机验证：`acli log_collect create --help` 会以 exit code 1 结束
	// （插件把 --help 判为未定义参数），但正文已完整打印，因此只认输出内容、不认退出码。
	helpOut, _, _ := s.RunCommandPty(ctx, logCollectAcliInvoke("log_collect create --help"), 8*time.Second)
	// 同一份 help 文本顺便回答"能不能给上传请求加头"：
	// P1 直传要过 5888 的鉴权，全靠 `-L|--url-header` 注入票据。
	// 老版本没有这个参数，结果只能落 P2，必须让前端提前知道而不去撞 401。
	result.AcliSupportsUploadHeader = strings.Contains(helpOut, "--url-header") ||
		strings.Contains(helpOut, "--header")
	if modules := parseLogCollectModuleCatalog(helpOut); len(modules) > 0 {
		result.Modules = modules
		result.CatalogSource = "acli_help"
		// 回传节点真实枚举：目录恒补白名单全集，help 未列出的专项可能因版本
		// 不匹配被拒（Q2026111700614：1.0.0 无 core）。前端据此做非阻塞警示。
		result.NodeModules = parseLogCollectHelpModules(helpOut)
		if len(result.NodeModules) > 0 {
			listed := map[string]bool{}
			for _, value := range result.NodeModules {
				listed[value] = true
			}
			var unlisted []string
			for _, option := range modules {
				if !listed[option.Value] {
					unlisted = append(unlisted, option.Value)
				}
			}
			if len(unlisted) > 0 {
				result.CatalogNote = appendProbeNote(result.CatalogNote,
					fmt.Sprintf("节点 help 未列出专项 %s，可能因版本不匹配被拒", strings.Join(unlisted, ", ")))
			}
		}
	} else {
		result.Modules = builtinLogCollectModules()
		result.CatalogSource = "platform_default"
		result.CatalogNote = "acli 帮助解析失败，使用平台内置场景目录"
	}

	nodeOut, _, _ := s.RunCommandPty(ctx, logCollectAcliInvoke("--formatter json platform node list"), 8*time.Second)
	result.Hosts = parseLogCollectNodeList(nodeOut)
	if len(result.Hosts) == 0 {
		// 主命令失败不等于"没有节点"：旧版 acli（1.0.0）会因 cgroup 读取失败只输出错误行。
		// 备选命令 `host list` 在部分构建中存在；不存在时只是多一条失败输出，
		// 不影响主流程，也不会把"命令缺失"误报成"节点为空"。
		if altOut, _, altErr := s.RunCommandPty(ctx, logCollectAcliInvoke("--formatter json host list"), 8*time.Second); altErr == nil {
			if altHosts := parseLogCollectNodeList(altOut); len(altHosts) > 0 {
				result.Hosts = altHosts
				result.CatalogNote = appendProbeNote(result.CatalogNote, "节点清单取自备选命令 host list")
			}
		}
	}
	if len(result.Hosts) == 0 {
		// 不给客户看 stderr 原文：实机会把 acli 的 JSON 运行日志、Go 栈、cgroup 路径
		// 一起带出来，真因（节点清单拉取失败）反而被埋，还泄露实现细节。失败只在前端
		// 提示"可手填 IP"，原始输出留在桥日志供排查。
		result.CatalogNote = appendProbeNote(result.CatalogNote,
			"节点清单获取失败，可手动填写 IP（详细错误见桥日志）")
	}
}

// appendProbeNote 拼接提示文案，避免用空串拼出多余分隔符。
func appendProbeNote(current, extra string) string {
	if strings.TrimSpace(current) == "" {
		return extra
	}
	return current + "；" + extra
}

// logCollectHelpEnumBlock 从 help 文本圈定枚举区，返回目录解析与真实枚举统计共用。
//
// 中文 help 用「枚举值 … 说明」两段标题圈定枚举区；25 节点英文 help 的实机
// 标题是「Available modules, default is other, enumerate:」，后面用「Note:」
// 收尾（中间夹着 -d/-i 等参数行，但不会再出现白名单括号组合）。两者都按
// 「命中任一标题即圈定、命中任一收尾即截断」处理，找不到标题时仍回退全量扫描，
// 并把命中门槛提高到 2 条——示例命令里也可能出现括号，单条命中不足以定论。
func logCollectHelpEnumBlock(raw string) (string, bool) {
	block := raw
	scoped := false
	for _, head := range []string{"枚举值", "Enum values", "Valid values", "enumerate:"} {
		idx := strings.Index(block, head)
		if idx < 0 {
			continue
		}
		block = block[idx+len(head):]
		for _, tail := range []string{"说明", "Description", "Note:"} {
			if end := strings.Index(block, tail); end >= 0 {
				block = block[:end]
				break
			}
		}
		scoped = true
		break
	}
	return block, scoped
}

// parseLogCollectHelpModules 返回 help 中真实列出的白名单专项（按目录顺序、去重）。
//
// 与 parseLogCollectModuleCatalog 的"补齐白名单全集"不同，这里只报事实：
// help 未列出的专项可能因版本不匹配被节点拒绝（acli 1.0.0 无 core），
// 前端据此渲染非阻塞警示，不影响可选性——help 漏列但实际支持的版本不能被误伤。
func parseLogCollectHelpModules(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	block, _ := logCollectHelpEnumBlock(raw)
	hit := map[string]bool{}
	for _, match := range logCollectModuleOptionRe.FindAllStringSubmatch(block, -1) {
		if logCollectModules[match[1]] {
			hit[match[1]] = true
		}
	}
	if len(hit) == 0 {
		return nil
	}
	out := make([]string, 0, len(hit))
	for _, value := range logCollectModuleCatalogOrder {
		if hit[value] {
			out = append(out, value)
		}
	}
	return out
}

// parseLogCollectModuleCatalog 从 help 文本提取场景枚举。
//
// 对抗性审查：实机 help 把枚举值按两列排版（同一行有两个「名称（标签）」），
// 且正文里还夹着示例命令，因此只解析「枚举值」到「说明」之间的段落并全量扫描。
func parseLogCollectModuleCatalog(raw string) []logCollectModuleOption {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	block, scoped := logCollectHelpEnumBlock(raw)

	// 第一性原理：白名单 logCollectModules 是 -m 的合法枚举全集，目录只应从中取。
	// help 只用来「确认集群确实支持枚举」，不用来生成展示文案：
	//   - help 的说明文字（"约4min"、""不收与上述n个场景"）是参数释义，当选项名会不可读；
	//   - help 会漏列合法枚举（实机 acli 1.0.1 未列出 core），漏列的专项用户永远看不到。
	// 因此 label 一律取自平台内置短名 logCollectModuleLabels，并按固定顺序补齐白名单全集。
	matches := logCollectModuleOptionRe.FindAllStringSubmatch(block, -1)
	// 只统计命中白名单的枚举：英文 help 正文里 `file (Please ...)` 这类半角括号
	// 也会被正则命中，按条数判断会把"没有枚举的文本"误认成集群目录，
	// 让目录来源提示谎称来自集群。
	validMatches := 0
	for _, match := range matches {
		if logCollectModules[match[1]] {
			validMatches++
		}
	}
	// 枚举解析不到，说明 help 改版/乱码：交给调用方回退内置目录，
	// 而不是拿全量白名单冒充"集群目录"（那样目录来源提示会谎称来自集群）。
	minMatches := 1
	if !scoped {
		minMatches = 2
	}
	if validMatches < minMatches {
		return nil
	}

	options := make([]logCollectModuleOption, 0, len(logCollectModuleCatalogOrder))
	for _, value := range logCollectModuleCatalogOrder {
		if !logCollectModules[value] {
			continue
		}
		options = append(options, logCollectModuleOption{
			Value: value,
			Label: logCollectModuleLabels[value],
		})
	}
	return options
}

// parseLogCollectNodeList 解析 `acli --formatter json platform node list` 的输出。
//
// 阶段 2 A3：脏输出容错解析已下沉到 shared/cluster（acli 插件同步要同一份清单），
// 这里只保留委托，避免两处口径不一致。
func parseLogCollectNodeList(raw string) []logCollectHostOption {
	return cluster.ParseNodeList(raw)
}

// 节点态/主机态枚举与结构化视图的别名：类型与取值归 shared/acliout，
// 采集编排与状态机继续用域内名字，避免大规模改名噪音。
type logCollectProgress = acliout.Progress
type logCollectHost = acliout.Host

const (
	logCollectNodeNone      = acliout.NodeNone
	logCollectNodeRunning   = acliout.NodeRunning
	logCollectNodeCompleted = acliout.NodeCompleted
	logCollectNodeAbnormal  = acliout.NodeAbnormal
	logCollectNodeUnknown   = acliout.NodeUnknown

	logCollectHostRunning   = acliout.HostRunning
	logCollectHostCompleted = acliout.HostCompleted
	logCollectHostFailed    = acliout.HostFailed
)

// classifyLogCollectNodeState 保留为域内薄包装：归一化实现已归 shared/acliout，
// 交付域解析 progress 时同样需要同一套判据（否则两处会对同一份输出给出不同状态）。
func classifyLogCollectNodeState(progress logCollectProgress) string {
	return acliout.ClassifyNodeState(progress)
}

// classifyLogCollectHostStatusWord 保留为域内薄包装（实现归 shared/acliout）。
func classifyLogCollectHostStatusWord(word string) string {
	return acliout.ClassifyHostStatusWord(word)
}

// isLogCollectFailureLine 保留为域内薄包装（实现归 shared/acliout）。
func isLogCollectFailureLine(line string) bool {
	return acliout.IsFailureLine(line)
}

// ── create 下发后的节点接单确认 ──────────────────────────────────────────────
//
// 第一性原理：exit_code=0 只证明"进程结束了"，不证明"节点建了任务"。
// PTY 通道把 ssh.ExitError 归一化成 nil，acli 包装脚本又会吞掉内部异常，
// 现场 Q2026110901673 因此出现「create 成功、progress 持续回收集任务不存在」。
// 唯一可自证的正向证据是：create 之后节点真的出现 running（采集起点）。
const (
	logCollectCreateAckAttempts = 3
	logCollectCreateAckInterval = 2 * time.Second
	// 确认总预算必须远小于 HandleLogCollect 的 180s 主预算：create 本身最长 180s，
	// 若确认也占用同等预算，整条链路会被 ctx 掐断在确认阶段。
	logCollectCreateAckBudget = 12 * time.Second
)

const (
	logCollectAckConfirmed      = "confirmed"
	logCollectAckNotEstablished = "not_established"
	// 「没确认到」不等于「确认失败」：查询超时/通道抖动时保持未知，
	// 由前端的有界等待兜底，绝不把一次正常采集判成失败。
	logCollectAckUnknown = "unknown"
)

type logCollectCreateAckResult struct {
	State  string   `json:"state"`
	States []string `json:"states"`
}

// confirmLogCollectCreate 有界查询 progress，确认本次 create 是否真的被节点接单。
//
// run 由调用方注入（单测无需真实 SSH）。结论三态，判据从保守到确定：
//   - 见到 running → confirmed（只认起点，不认残留的 completed 报告）；
//   - 见到 abnormal，或每次都明确回报「无任务」→ not_established；
//   - 查询失败、超时、出现 completed/unknown → unknown，不替节点下失败结论。
func confirmLogCollectCreate(
	ctx context.Context,
	interval time.Duration,
	run func(context.Context) (string, int, error),
) logCollectCreateAckResult {
	ackCtx, cancel := context.WithTimeout(ctx, logCollectCreateAckBudget)
	defer cancel()
	result := logCollectCreateAckResult{State: logCollectAckUnknown}
	queryFailed := false
	allNone := true
	for i := 0; i < logCollectCreateAckAttempts; i++ {
		if i > 0 {
			select {
			case <-ackCtx.Done():
				return result
			case <-time.After(interval):
			}
		}
		if ackCtx.Err() != nil {
			return result
		}
		output, exitCode, err := run(ackCtx)
		if err != nil || exitCode != 0 {
			queryFailed = true
			result.States = append(result.States, "query_error")
			continue
		}
		state := classifyLogCollectNodeState(ParseLogCollectProgress(output))
		result.States = append(result.States, state)
		switch state {
		case logCollectNodeRunning:
			result.State = logCollectAckConfirmed
			return result
		case logCollectNodeAbnormal:
			result.State = logCollectAckNotEstablished
			return result
		case logCollectNodeNone:
			// 继续观察：create 是同步命令，但插件注册任务可能有秒级延迟。
		default:
			// completed 可能是上一轮任务的残留报告（acli 节点级单任务、progress 不带任务 ID，
			// 见 Q2026110666667）；unknown 说明解析降级。两者都不能用来判失败。
			allNone = false
		}
	}
	if !queryFailed && allNone && len(result.States) == logCollectCreateAckAttempts {
		result.State = logCollectAckNotEstablished
	}
	return result
}

// ParseLogCollectProgress 保留为域内薄包装：解析实现已归 shared/acliout
// （交付域同样要从 progress 输出取归档路径，不能各自实现一套）。
func ParseLogCollectProgress(raw string) logCollectProgress {
	return acliout.ParseProgress(raw)
}

// ── 源包摘要与归档探测（薄包装）─────────────────────────────────────────────
//
// 阶段 2 A3：实现已归 shared/remote（ProbeSourceDigest / ProbeArchive /
// NormalizeArchivePath）。交付域拉包前要确认包还在、拉完后要核对摘要，
// 若继续从采集域借用，交付域就得为两个探测函数反向依赖采集编排。
// 这里保留同名薄包装，使采集链路与既有测试无需改动。

// archiveProbe 是远端正探测结果的三态载体（类型别名，字段语义见 remote.ArchiveProbe）。
type archiveProbe = remote.ArchiveProbe

// ProbeSourceDigest 在节点上取一次源包摘要（尽力而为，不阻断交付）。
func ProbeSourceDigest(r remote.Runner, ctx context.Context, path string) (string, string, error) {
	return remote.ProbeSourceDigest(ctx, r, path)
}

// ProbeLogCollectArchive 量一次压缩包状态（大小 + 三态存在性）。
func ProbeLogCollectArchive(ctx context.Context, r remote.StreamsRunner, rawPath string) archiveProbe {
	return remote.ProbeArchive(ctx, r, rawPath)
}

// truncateForDiag 截短命令输出，避免把整段终端噪音塞进日志。
func truncateForDiag(text string, limit int) string {
	trimmed := strings.TrimSpace(text)
	if len(trimmed) <= limit {
		return trimmed
	}
	return trimmed[:limit] + "..."
}

// fallbackLogCollectArchivePath 在插件还没打印路径时，按约定推导压缩包位置。
//
// 第一性原理：压缩包大小是「文件存在期间」的物理属性，量它只需要路径，不需要插件同意。
// 而现场数据（case Q2026101401683，staging bridge_execution_logs）显示：
//
//	11:13:20  progress(85%, 上传中)   archive_path = (空)
//	11:38:28  progress(100%, 已成功)  archive_path = /sf/data/local/acli/tmp/data/log_collect/Q2026101401683.zip
//
// 也就是说插件在「进行中 / 上传中」阶段只打印「压缩包路径（完成日志下载后请手动删除压缩包）：」
// 这一行标题、冒号后面是空的，直到 100% 完成才给出真实路径——而那时 P1 已上传成功、
// 插件已自删包，stat 必然失败。于是 40 条 progress 事件的 archive_size 全为空：
// 不是"忘了存"，是"从来没量到过"。
//
// 路径本身是可推导的：{case_id}.zip 固定落在 log_collect 目录下（P1 成功后自删，
// 不会留同名残包）。case_id 会进 shell 命令，因此拼完必须再过一遍
// logCollectArchivePathPattern（白名单 [A-Za-z0-9_.-]），不合法就放弃推导。
func fallbackLogCollectArchivePath(caseID string) string {
	trimmed := strings.TrimSpace(caseID)
	if trimmed == "" {
		return ""
	}
	path := "/sf/data/local/acli/tmp/data/log_collect/" + trimmed + ".zip"
	if !remote.ArchivePathPattern.MatchString(path) {
		return ""
	}
	return path
}

// logCollectArchiveDir 是 acli 的产物目录；归档子目录放在它下面，避免与插件可能的历史 glob 撞车。
const logCollectArchiveDir = "/sf/data/local/acli/tmp/data/log_collect"

// archiveStaleLogCollectArchive 在 create 之前把上一次留下的同名包改名归档。
//
// 为什么必须做：产物路径由 acli 固定为 `{logCollectArchiveDir}/{case_id}.zip`，
// 同一工单重新采集必然**同名覆盖**。现场表现为"刚采好的包被新任务删掉了"，
// 而它正是补交付失败的物理原因——用户点上传时，磁盘上已是新任务的半成品。
//
// 为什么用 mv 不用 cp：同目录 rename 不占额外空间（现场包可达 20GB），
// 且不改 acli 的写入语义——它仍按固定路径新建，我们只是先给旧文件让路。
//
// 失败不阻断：归档是"尽力而为"的保全动作，没权限/目录不存在都不应让采集停下。
func archiveStaleLogCollectArchive(ctx context.Context, s *sshx.SSHSession, caseID, traceID, cui string) {
	path := fallbackLogCollectArchivePath(caseID)
	if path == "" {
		return
	}
	stamp := time.Now().Format("20060102150405")
	trimmed := strings.TrimSpace(caseID)
	target := logCollectArchiveDir + "/archive/" + trimmed + "-" + stamp + ".zip"
	// 目标路径同样进 shell，重跑一次白名单校验（不能只信来源）。
	if !remote.ArchivePathPattern.MatchString(target) {
		return
	}
	command := fmt.Sprintf(
		`S=%s; if [ -f "$S" ]; then mkdir -p %s && mv "$S" %s && echo "__ARCHIVED__ %s" || echo "__ARCHIVE_FAILED__"; else echo "__NO_ARCHIVE__"; fi`,
		remote.ShellQuote(path), remote.ShellQuote(logCollectArchiveDir+"/archive"), remote.ShellQuote(target), target,
	)
	output, _, err := s.RunQuickCommand(ctx, command)
	if err != nil {
		evidence.Emit("WARN", "log_collect.archive_failed", "归档旧压缩包失败（不阻断采集）", traceID, caseID, "", cui, map[string]any{
			"source": path, "error": errString(err),
		})
		return
	}
	trimmedOut := strings.TrimSpace(output)
	evidence.Emit("INFO", "log_collect.archive_checked", "采集前检查旧压缩包", traceID, caseID, "", cui, map[string]any{
		"source": path, "result": trimmedOut,
	})
}

// stripAnsi 去掉终端转义序列：保留可读文本，避免解析与展示被颜色码污染。
// 唯一实现在 remote.StripANSI（正则与收口前逐字一致）。
func stripAnsi(text string) string {
	return remote.StripANSI(text)
}

// logCollectFailureLine 从 acli 输出里提取一条可读的失败原因。
//
// 背景：RunCommandPty 把 ssh.ExitError 归一化成 exitCode 后返回 nil，单看
// errString(err) 永远是空串（2026-10-27 实测 19 条失败 create 全部 error=""），
// 导致桥侧日志、平台任务与前端提示都拿不到原因。
//
// 取法（2026-11-12 修正）：FATA 只是 acli 的包装行，不是原因本身。
// 旧实现一遇到 FATA 就返回，导致「收集专项core不存在！」被丢掉，
// 前端 normalize 再把唯一一行 FATA 过滤成空串，用户只看到"日志收集失败"。
// 新优先级：业务关键词行（中/英）> 首个非噪音行 > FATA 包装行。
func logCollectFailureLine(output string) string {
	cleaned := stripAnsi(output)
	var firstLine, fatalLine string
	for _, raw := range strings.Split(cleaned, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		// 时间戳只是日志前缀，剥掉后内容仍要参与判定：
		// 直接整行跳过会把「2026/11/12 10:00:00 收集专项core不存在！」一起丢掉。
		line = strings.TrimSpace(logCollectTimestampRe.ReplaceAllString(line, ""))
		line = strings.TrimSpace(strings.TrimLeft(line, "-|[] "))
		if line == "" {
			continue
		}
		// JSON / logfmt 结构化日志是 acli 的噪声行，不含用户可读的原因。
		if strings.HasPrefix(line, "{") {
			continue
		}
		if strings.HasPrefix(line, "FATA") || strings.Contains(line, "level=fatal") ||
			strings.Contains(line, "level=error") {
			if fatalLine == "" {
				fatalLine = line
			}
			continue
		}
		if firstLine == "" {
			firstLine = line
		}
		if logCollectIsReasonLine(line) {
			return sshx.TruncateString(line, 200)
		}
	}
	if firstLine != "" {
		return sshx.TruncateString(firstLine, 200)
	}
	return sshx.TruncateString(fatalLine, 200)
}

// logCollectIsReasonLine 判断一行是不是"业务原因"。
// 中英双语都要覆盖：客户现场 acli 可能跑在英文 locale 下，
// 只认中文关键词会让英文环境的失败原因退化成"首个非空行"（往往是进度条/表头）。
func logCollectIsReasonLine(line string) bool {
	for _, kw := range []string{"错误", "失败", "不存在", "不支持", "未找到", "非法", "权限", "超时"} {
		if strings.Contains(line, kw) {
			return true
		}
	}
	lower := strings.ToLower(line)
	for _, kw := range []string{"error", "failed", "failure", "not found", "not supported",
		"permission denied", "timeout", "timed out", "no such file", "read-only"} {
		if strings.Contains(lower, kw) {
			return true
		}
	}
	return false
}

// logCollectModuleMissing 识别 acli 的「收集专项 X 不存在」。
// 这不是普通的命令非零退出：处置动作是核对专项名/acli 版本，重试不会变好。
func logCollectModuleMissing(output string) bool {
	cleaned := stripAnsi(output)
	return strings.Contains(cleaned, "收集专项") && strings.Contains(cleaned, "不存在")
}
