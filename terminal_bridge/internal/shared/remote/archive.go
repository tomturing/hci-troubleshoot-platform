package remote

// 节点侧归档包的物理事实：路径白名单、存在性三态、源包摘要。
//
// 阶段 2 A3 之前这三组原语住在 logcollect，交付域为了"拉包前确认包还在、
// 拉完后核对摘要"不得不反向依赖采集域。它们只认识 Runner 与远端命令，
// 不认识采集编排，按审查文档的修复范式（摘要/探测/拉取 → remote/uplink）
// 归位到节点面横向原语。

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"terminal_bridge/internal/shared/aclibin"
)

const (
	// digestTimeout：20 GiB 的 sha256 在 HCI 上约 1~3 分钟；留足余量但不能无限等：
	// 交互式 shell 的 TMOUT 是 600s，超过它的单命令会被远端直接踢掉。
	digestTimeout = 8 * time.Minute

	// logCollectArchiveDir 是插件产物固定目录；HCI ≥6.12 的 progress 会把前缀
	// 改写为 /support，需还原。
	logCollectArchiveDir = "/sf/data/local/acli/tmp/data/log_collect/"
)

var (
	// digestAlgoRe 只接受两种摘要形态：sha256(64) / md5(32)（acli 段也是 md5）。
	digestAlgoRe = regexp.MustCompile(`^(sha256|md5|acli) ([0-9a-f]{32}|[0-9a-f]{64})$`)

	// ArchivePathPattern 是归档路径白名单：只接受插件约定目录下的 .zip，
	// 且文件名不带任何 shell 元字符。
	ArchivePathPattern = regexp.MustCompile(`^/sf/data/local/acli/tmp/data/log_collect/[A-Za-z0-9_.-]+\.zip$`)

	// ArchiveProbeToolMissingRe 匹配"命令不可用"（命令不存在、被受限 shell 拒绝、
	// 或不吃这个参数）。与"文件不存在"必须分开，否则会把盘上的包判成已删除。
	ArchiveProbeToolMissingRe = regexp.MustCompile(`(?i)(command not found|: not found|unknown command|not supported|invalid option|illegal option|permission denied)`)

	// ArchiveProbeCommandMissingRe 只匹配「命令本身不存在/不认识这个参数」。
	// 刻意不含 permission denied：权限拒绝要改权限，缺命令要放开白名单，
	// 两者指向的处理动作不同，混成一个码会把客户引到错误方向。
	ArchiveProbeCommandMissingRe = regexp.MustCompile(
		`(?i)(command not found|: not found|unknown command|not supported|invalid option|illegal option)`,
	)

	// archiveProbeFileMissingRe 匹配"文件不存在"：只有拿到这类明确证据才允许下结论。
	archiveProbeFileMissingRe = regexp.MustCompile(`(?i)(no such file|cannot access|cannot stat|does not exist|not exist)`)
)

// NormalizeArchivePath 校验并归一化 HCI 上的压缩包路径。
//
// 只接受插件约定的目录，且不允许任何 shell 元字符——该路径最终会进入 shell 命令
// （`tail -c +N <path>`），虽然经过 ShellQuote，仍先做白名单收敛。
func NormalizeArchivePath(raw string) (string, error) {
	path := strings.TrimSpace(raw)
	if path == "" {
		return "", fmt.Errorf("source_path 不能为空")
	}
	// HCI ≥6.12：progress 展示的 /support/... 实际对应 /sf/data/local/acli/...
	if strings.HasPrefix(path, "/support/") {
		path = "/sf/data/local/acli/" + strings.TrimPrefix(path, "/support/")
	}
	if !ArchivePathPattern.MatchString(path) {
		return "", fmt.Errorf("source_path 必须是 %s 下的 .zip 文件", logCollectArchiveDir)
	}
	return path, nil
}

// ArchiveProbe 是「节点上这个压缩包此刻是什么状态」的唯一结论载体（三态）。
//
// 为什么必须是三态而不是 bool：Q2026102900165（2026-10-29）现场证明，
// aSV 受限 shell 里根本没有 stat（exit 127，包却真在盘上、29.9 MB），
// 而 ls -l 可用。旧实现把「命令跑不起来」与「文件真没了」压成同一个 exists=false，
// 平台于是对着盘上的包断言「压缩包已被插件删除」。界面只能表达已知与未知，
// 不能把未知伪装成已知，所以这里必须把「无法确定」单独留一个出口。
type ArchiveProbe struct {
	// Size 为 0 表示没量到（不代表 0 字节）；交付 init 需要它是正数。
	Size int64
	// Exists：true=确认在盘上；false=确认不在；nil=无法确定（命令不可用/通道异常）。
	Exists *bool
	// Level 记录结论由哪一级命令给出：stat / ls / missing / none。
	Level string
	// Unavailable 表示「无法确认」的原因是两级命令都跑不起来（126/127 或命令缺失文本），
	// 而不是通道异常或权限拒绝。前者要客户恢复节点命令，后者要重连；混成一个码会让
	// 现场按错误的方向处理（Q2026102900165 的 stat exit 127 曾被当成「包已被删除」）。
	Unavailable bool
	// Diag 非空即表示有过失败尝试（走了兜底或全失败），必须随事件落库。
	Diag string
}

// archiveProbeAttempt 是一次单命令探测的原始结果（stdout/stderr 必须分开）。
type archiveProbeAttempt struct {
	Stdout string
	Stderr string
	Code   int
	Err    error
}

func (a archiveProbeAttempt) succeeded() bool {
	return a.Err == nil && a.Code == 0
}

// saidMissing 判断 stderr/stdout 是否明确说「没有这个文件」。
func (a archiveProbeAttempt) saidMissing() bool {
	return archiveProbeFileMissingRe.MatchString(a.Stdout + "\n" + a.Stderr)
}

// unavailable 判断这一级是否"命令本身跑不起来"。
//
// 126/127 是 shell 对「找到但不能执行 / 找不到」的固定约定，必须优先于文本匹配：
// busybox 的 "stat: not found" 与 "No such file" 是两件完全不同的事。
func (a archiveProbeAttempt) unavailable() bool {
	if a.Code == 126 || a.Code == 127 {
		return true
	}
	return ArchiveProbeToolMissingRe.MatchString(a.Stdout + "\n" + a.Stderr)
}

// commandMissing 判断这一级是不是「命令不存在/不被支持」，用于把 LC-ENV-009
// （节点缺命令）从一团「无法确认」里单列出来。
//
// 与 unavailable 的分工：unavailable 为了安全侧把权限拒绝也算进来（宁可说
// 「测不到」也不说「文件没了」），但归因必须更严格——权限拒绝落在这里就会
// 让客户去放开命令白名单，而真正要改的是目录权限。
func (a archiveProbeAttempt) commandMissing() bool {
	if a.Code == 126 || a.Code == 127 {
		return true
	}
	return ArchiveProbeCommandMissingRe.MatchString(a.Stdout + "\n" + a.Stderr)
}

func (a archiveProbeAttempt) diag(name string) string {
	detail := a.Stderr
	if detail == "" {
		detail = a.Stdout
	}
	detail = truncateForDiag(detail, 120)
	if detail == "" {
		return fmt.Sprintf("%s:exit=%d", name, a.Code)
	}
	return fmt.Sprintf("%s:exit=%d %s", name, a.Code, detail)
}

// decideArchiveProbe 把两级命令结果收敛成三态结论（纯函数，便于穷举测试）。
//
// 判据顺序经过对抗性审查：
//  1. 任一级读取成功 => 文件确定存在（成功读盘本身就否证了“不存在”）；
//  2. 读不到时，只有拿到“没有这个文件”的明确证据才判 false；
//  3. 其余（命令不存在、权限、通道异常）一律 nil——宁可说“无法确认”，
//     也不能把“我探测不了”就成了“包被删了”。
func decideArchiveProbe(statA, lsA archiveProbeAttempt) ArchiveProbe {
	var diags []string
	if !statA.succeeded() {
		diags = append(diags, statA.diag("stat"))
	}
	if !lsA.succeeded() {
		diags = append(diags, lsA.diag("ls"))
	}
	probe := ArchiveProbe{Diag: strings.Join(diags, " | ")}
	exists := true

	if statA.succeeded() {
		if size, ok := parseSizeText(statA.Stdout); ok && size > 0 {
			probe.Size, probe.Level, probe.Exists = size, "stat", &exists
			return probe
		}
	}
	if lsA.succeeded() {
		probe.Level = "ls"
		if size, ok := parseLsFieldSize(lsA.Stdout); ok {
			probe.Size = size
		}
		probe.Exists = &exists
		return probe
	}
	if statA.succeeded() {
		// stat 成功但大小解析不出：存在性是确定的，只是大小未知。
		probe.Level, probe.Exists = "stat", &exists
		return probe
	}
	// 只有「命令确实跑了、并且明确说没有这个文件」的那一级才有资格判不存在。
	// 若把 exit 127 那级的 stderr 也算进来，受限 shell 的 "not found" 就又会
	// 被误当成"文件没了"——这正是本缺陷的原始形态。
	if saidMissingBy(statA) || saidMissingBy(lsA) {
		absent := false
		probe.Level, probe.Exists = "missing", &absent
		return probe
	}
	// 其余（命令不可用、权限、通道异常、上下文取消）一律未知：
	// 平台宁可显示「无法确认」，也不能替用户断言包已被删除。
	// 再把「命令不可用」从这团未知里单列出来：只有两级都判命令缺失才算环境问题，
	// 一级缺失 + 一级超时仍然指向通道异常，不能算到节点环境头上。
	probe.Level = "none"
	probe.Unavailable = statA.commandMissing() && lsA.commandMissing()
	return probe
}

// saidMissingBy 判断某一级是否给出了「文件不存在」的可靠证据。
func saidMissingBy(a archiveProbeAttempt) bool {
	return !a.succeeded() && !a.unavailable() && a.saidMissing()
}

// ProbeArchive 量一次压缩包状态（大小 + 三态存在性）。
//
// 时机敏感：P1 直传成功后插件会立刻删除压缩包，只有上传窗口内的轮询能拿到值。
// 量不到不报错、不阻断进度查询，返回 0 由前端显示「未记录」。
//
// 两级降级的必要性见 decideArchiveProbe：aSV 受限 shell 只给了一份受控命令白名单，
// stat 不在其中而 ls 在。两级结果与逐级诊断（含 exit 与 stderr）一并回给调用方，
// 现场再遇到“面板说包没了”时能直接指出是哪一级、错在哪里。
func ProbeArchive(ctx context.Context, r StreamsRunner, rawPath string) ArchiveProbe {
	if r == nil {
		return ArchiveProbe{Level: "none", Diag: "no-session"}
	}
	if strings.TrimSpace(rawPath) == "" {
		return ArchiveProbe{Level: "none", Diag: "empty-path"}
	}
	path, err := NormalizeArchivePath(rawPath)
	if err != nil {
		return ArchiveProbe{Level: "none", Diag: "path-rejected: " + err.Error()}
	}
	statOut, statErr, statCode, statCmdErr := r.RunQuickCommandStreams(ctx, fmt.Sprintf("stat -c %%s %s", ShellQuote(path)))
	lsOut, lsErr, lsCode, lsCmdErr := r.RunQuickCommandStreams(ctx, fmt.Sprintf("ls -l %s", ShellQuote(path)))
	return decideArchiveProbe(
		archiveProbeAttempt{Stdout: statOut, Stderr: statErr, Code: statCode, Err: statCmdErr},
		archiveProbeAttempt{Stdout: lsOut, Stderr: lsErr, Code: lsCode, Err: lsCmdErr},
	)
}

// parseSizeText 解析 stat -c %s 的纯数字输出。
func parseSizeText(output string) (int64, bool) {
	size, err := strconv.ParseInt(strings.TrimSpace(output), 10, 64)
	if err != nil || size < 0 {
		return 0, false
	}
	return size, true
}

// parseLsFieldSize 从 `ls -l` 的单行输出里取字节数（第 5 列）。
//
// 只解析单行、只认正整数：多行说明路径被当成通配符展开过，此时宁可放弃也不猜，
// 免得把目录里另一个文件的大小当成压缩包大小记下来。
func parseLsFieldSize(output string) (int64, bool) {
	trimmed := strings.TrimSpace(output)
	// 行数不为 1 说明路径被通配符展开、或命令在报错行里也打了内容，此时不猜
	if trimmed == "" || strings.Count(trimmed, "\n") > 0 {
		return 0, false
	}
	fields := strings.Fields(trimmed)
	if len(fields) < 5 {
		return 0, false
	}
	size, err := strconv.ParseInt(fields[4], 10, 64)
	if err != nil || size <= 0 {
		return 0, false
	}
	return size, true
}

// buildSourceDigestCommand 构造"取源包摘要"的单命令。
//
// 第一性原理：能证明"节点上的包 = 平台副本 = 5888 归档件"的锚点只能在源端产生；
// 分片哈希只证明单跳不损坏，证明不了上游是谁。
//
// 为什么优先 sha256sum：链路其余环节（分片、平台合并）全是 SHA-256，同算法才能
// 直接比对；md5 只是老节点的降级手段。为什么最后才退 acli system md5sum：它的
// 参数形态随插件版本变化，1.0.0 上未必存在，而前两者是标准 coreutils/busybox。
//
// 失败语义：任何一步都算不出来就输出空行，由调用方把完整性降级为 unverified——
// 绝不能因为"取不到摘要"就让一次 20 GiB 的采集白跑。
func buildSourceDigestCommand(path string) string {
	quoted := ShellQuote(path)
	return aclibin.PathEnv + " " + aclibin.ResolveScript() +
		` if command -v sha256sum >/dev/null 2>&1; then echo "sha256 $(sha256sum ` + quoted + ` 2>/dev/null | awk '{print $1}')"; ` +
		`elif command -v md5sum >/dev/null 2>&1; then echo "md5 $(md5sum ` + quoted + ` 2>/dev/null | awk '{print $1}')"; ` +
		`elif [ "$__acli_resolve_absent" = "0" ]; then echo "acli $("$__acli" system md5sum ` + quoted + ` 2>/dev/null | tr -d ' \r' | grep -oE '[0-9a-f]{32}' | head -n1)"; ` +
		`else echo ""; fi`
}

// parseSourceDigestOutput 解析摘要命令输出。
//
// 从后往前找第一条合法结果：acli 解析段可能打印版本/告警噪声，最后一条有效行
// 才是真正的结果。算法与长度必须自洽，防止把 32 位 md5 当成 sha256 用。
func parseSourceDigestOutput(output string) (algo, digest, note string) {
	lines := strings.Split(strings.TrimSpace(output), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(StripANSI(lines[i]))
		match := digestAlgoRe.FindStringSubmatch(line)
		if match == nil {
			continue
		}
		algo, digest = match[1], match[2]
		if algo == "sha256" && len(digest) != 64 {
			continue
		}
		if (algo == "md5" || algo == "acli") && len(digest) != 32 {
			continue
		}
		if algo == "acli" {
			// acli system md5sum 只有 md5 口径，算法名必须归一到 md5。
			algo = "md5"
		}
		return algo, digest, ""
	}
	trimmed := strings.TrimSpace(output)
	if len(trimmed) > 200 {
		trimmed = trimmed[:200]
	}
	if trimmed == "" {
		trimmed = "节点上没有可用的摘要命令（sha256sum / md5sum / acli system md5sum 均不可用）"
	}
	return "", "", trimmed
}

// ProbeSourceDigest 在节点上取一次源包摘要（尽力而为，不阻断交付）。
func ProbeSourceDigest(ctx context.Context, r Runner, path string) (algo, digest string, err error) {
	if r == nil {
		return "", "", fmt.Errorf("no-session")
	}
	digestCtx, cancel := context.WithTimeout(ctx, digestTimeout)
	defer cancel()
	output, _, err := r.RunQuickCommand(digestCtx, buildSourceDigestCommand(path))
	if err != nil {
		return "", "", fmt.Errorf("摘要命令执行失败: %w", err)
	}
	algo, digest, note := parseSourceDigestOutput(output)
	if algo == "" {
		return "", "", fmt.Errorf("%s", note)
	}
	return algo, digest, nil
}

// truncateForDiag 截短命令输出，避免把整段终端噪音塞进日志。
// 与采集域的同名私有函数行为一致；两行函数不做跨包共享（设计 §3.5）。
func truncateForDiag(text string, limit int) string {
	trimmed := strings.TrimSpace(text)
	if len(trimmed) <= limit {
		return trimmed
	}
	return trimmed[:limit] + "..."
}
