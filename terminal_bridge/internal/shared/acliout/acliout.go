// Package acliout 解析 acli 插件的**文本输出**（progress / 节点明细 / 失败原因）。
//
// 为什么独立成包：与 aclibin（"怎么调 acli"）成对，这里回答"怎么读 acli 说的话"。
// 阶段 2 A3 之前它在 logcollect 里，交付域为了从 progress 输出取归档路径，
// 只能反向依赖采集编排。输出解析不认识采集状态机，也不认识交付作业，
// 属于两个域共用的事实层。
//
// 对抗性审查（保留自原实现）：
//   - 插件版本变化可能改文案，解析失败必须降级为 Raw 透传，而不是返回零值进度
//     导致前端误判"任务丢失"；
//   - 语言随运行环境变化（同一版本在中文/英文机器上输出不同），因此只锚定
//     「标签 + 冒号 + 值」、百分数、IP 这些**结构特征**，不逐条比对中文前缀。
package acliout

import (
	"regexp"
	"strconv"
	"strings"
)

// 节点态枚举：平台状态机的唯一输入。
const (
	NodeNone      = "none"
	NodeRunning   = "running"
	NodeCompleted = "completed"
	NodeAbnormal  = "abnormal"
	NodeUnknown   = "unknown"

	HostRunning   = "running"
	HostCompleted = "completed"
	HostFailed    = "failed"
)

// Host 是节点明细行（progress 里的逐台状态）。
type Host struct {
	IP      string `json:"ip"`
	Status  string `json:"status"`
	Percent string `json:"percent,omitempty"`
}

// Progress 是 acli log_collect progress 的结构化视图。
// ParseOK=false 时前端应原样展示 Raw，且不得据此把任务判为失败。
type Progress struct {
	Status      string `json:"status"`
	Percent     int    `json:"percent"`
	Done        int    `json:"done"`
	Total       int    `json:"total"`
	ArchivePath string `json:"archive_path"`
	TotalTime   string `json:"total_time,omitempty"`
	Hosts       []Host `json:"hosts,omitempty"`
	UploadURL   string `json:"upload_url,omitempty"`
	UploadState string `json:"upload_state,omitempty"`
	// UploadError 是上传失败时命中的首个原因行。
	//
	// 为什么需要它：2026-11-03 Q2026110300278 的 upload_state 只有"失败"两个字，
	// 平台与桥侧都无法区分 401 / 超时 / 网络，只能去 5888 翻服务端日志。
	UploadError string `json:"upload_error,omitempty"`
	// UploadRawTail 是上传阶段的输出尾部（仅在出现上传状态时填充）。
	// 结构化的失败文案不保证存在，原文尾部是排障的最后一条线索。
	UploadRawTail string `json:"upload_raw_tail,omitempty"`
	ParseOK       bool   `json:"parse_ok"`
	Raw           string `json:"raw"`
	// NodeState 是归一化后的节点态（none/running/completed/abnormal/unknown）。
	// 节点是任务状态的唯一真相源，但插件只给自然语言文案，平台无法直接拿它做状态机判断；
	// 归一化放在桥侧，避免每个前端各自实现一套字符串匹配。
	NodeState string `json:"node_state"`
	// ArchiveSize 是本次探测到的压缩包字节数；0 表示没量到（未生成 / 已被插件删除）。
	ArchiveSize int64 `json:"archive_size,omitempty"`
	// ArchiveExists 是存在性结论，必须与 ArchivePath 分开看：
	// 路径只说明"曾经生成过"，P1 直传成功后插件会立刻删包，报告里却仍带路径
	// （Q2026102701193，2026-10-27）。用指针是为了区分"明确不存在(false)"与"没探测(缺省)"——
	// 前端只在明确 true 时才允许"上传到平台"，缺省时一律不提供入口。
	ArchiveExists *bool `json:"archive_exists,omitempty"`
	// ArchiveResidual 表示盘上这个包在本次下发之前就存在、且采集期间从未被写过：
	// 它是同工单上一批次残留在固定路径上的产物（取消后改模块重收最容易踩到），
	// 因此 ArchiveSize 已被置 0，前端不得据此提供"上传到平台"。
	ArchiveResidual bool `json:"archive_residual,omitempty"`
}

var (
	// progressRe 按「标签 + 百分比 + 分数」匹配，中英文共用一条。
	progressRe = regexp.MustCompile(`^(?:总进度|Overall progress)\s*[:：]\s*(\d+)%\s*-\s*(\d+)\s*/\s*(\d+)`)
	// hostLineRe 匹配「行首 IP + 中间状态词 + 百分数」。
	hostLineRe = regexp.MustCompile(`^(\d{1,3}(?:\.\d{1,3}){3})\s+(.+?)\s+(\d+%|-)\s*$`)
)

// label 表示 progress 输出里「标签：值」这一行的标签，可含多种语言写法。
type label struct{ aliases []string }

var (
	statusLabel     = label{aliases: []string{"状态", "Status"}}
	uploadAddrLabel = label{aliases: []string{"发送地址", "Send address"}}
	archiveLabel    = label{aliases: []string{"压缩包路径", "Path to the compressed file"}}
	totalTimeLabel  = label{aliases: []string{"总耗时", "Total time"}}
)

// value 判断行是否以该标签开头，并返回冒号后的值。
//
// 分隔符只取标签后的**第一个**冒号：上传地址的值自带端口冒号
// （http://acli.sangfor.com.cn:5888），取最后一个会把地址切成 "5888"。
// 标签与冒号之间允许夹一段括号说明（英文的压缩包路径行就是这样）。
func (l label) value(line string) (string, bool) {
	for _, alias := range l.aliases {
		if len(line) < len(alias) || !strings.EqualFold(line[:len(alias)], alias) {
			continue
		}
		rest := line[len(alias):]
		// 英文标签后常带一个空格再接括号说明
		// （`Path to the compressed file (Please ...): /x.zip`），先剥掉再判分隔符。
		if trimmed := strings.TrimLeft(rest, " \t"); trimmed != "" {
			rest = trimmed
		}
		// 标签后必须紧跟冒号或括号说明，否则 "Statuses: ..." 会误命中 "Status"。
		if !(strings.HasPrefix(rest, "：") || strings.HasPrefix(rest, ":") ||
			strings.HasPrefix(rest, "（") || strings.HasPrefix(rest, "(")) {
			continue
		}
		idx := strings.IndexAny(rest, "：:")
		if idx < 0 {
			return "", true
		}
		value := strings.TrimPrefix(strings.TrimPrefix(rest[idx:], "："), ":")
		return strings.TrimSpace(value), true
	}
	return "", false
}

// ParseProgress 解析插件的 progress 文本输出。
func ParseProgress(raw string) Progress {
	result := Progress{Raw: raw}
	if strings.TrimSpace(raw) == "" {
		return result
	}

	// 任务状态与上传状态共用「状态/Status」标签，用「发送地址/Send address」出现位置区分上下文。
	sawUploadBlock := false
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if value, ok := uploadAddrLabel.value(line); ok {
			sawUploadBlock = true
			result.UploadURL = value
			continue
		}
		if value, ok := statusLabel.value(line); ok {
			if sawUploadBlock {
				result.UploadState = value
			} else {
				result.Status = value
			}
			continue
		}
		if value, ok := archiveLabel.value(line); ok {
			result.ArchivePath = value
			continue
		}
		if value, ok := totalTimeLabel.value(line); ok {
			result.TotalTime = value
			continue
		}
		if match := progressRe.FindStringSubmatch(line); match != nil {
			result.Percent, _ = strconv.Atoi(match[1])
			result.Done, _ = strconv.Atoi(match[2])
			result.Total, _ = strconv.Atoi(match[3])
			continue
		}
		// 只在上传块里抓第一行原因：任务块里的"失败"属于采集阶段，
		// 混进来会把"上传 401"显示成"采集失败"。
		if result.UploadError == "" && sawUploadBlock && IsFailureLine(line) {
			result.UploadError = truncate(line, 160)
			continue
		}
		// 行首 IP 不必然是节点明细，必须再由中间列归一化确认。
		if match := hostLineRe.FindStringSubmatch(line); match != nil &&
			ClassifyHostStatusWord(match[2]) != "" {
			result.Hosts = append(result.Hosts, Host{
				IP: match[1], Status: match[2], Percent: match[3],
			})
		}
	}
	result.ParseOK = result.Status != "" || result.Percent > 0 || result.ArchivePath != ""
	if result.UploadState != "" {
		// 上传阶段一旦有状态就留原文尾部：结构化失败文案不保证存在，
		// 而"为什么失败"是这一阶段唯一真正需要回答的问题。
		result.UploadRawTail = truncate(strings.TrimSpace(stripANSI(raw)), 512)
	}
	return result
}

// ClassifyNodeState 把插件的状态行归一为可做状态机判断的枚举。
//
// 对抗性审查：识别不了的一律 unknown——宁可不动平台状态，也不要按猜测去 cancel 节点任务。
// 「异常结束」必须单列：它既不含"完成"也不含"失败"，早期前端因此既不判完成也不判失败，
// 只能空转到 1 小时超时。
//
// 中英文必须同时覆盖：同一个 acli 版本在不同机器上输出语言不同（2026-11-06
// Q2026110486009 输出英文，旧实现只认中文，node_state 恒为 unknown）。
func ClassifyNodeState(progress Progress) string {
	status := strings.TrimSpace(progress.Status)
	lower := strings.ToLower(status)
	switch {
	case strings.Contains(status, "异常结束"), strings.Contains(status, "失败"), strings.Contains(status, "错误"),
		strings.Contains(lower, "fail"), strings.Contains(lower, "error"),
		strings.Contains(lower, "abnormal"):
		return NodeAbnormal
	case strings.Contains(status, "完成"),
		strings.Contains(lower, "complete"), strings.Contains(lower, "success"),
		strings.Contains(lower, "finish"), strings.Contains(lower, "done"):
		return NodeCompleted
	// 实机无任务文案是「收集任务不存在！」/ `Collect task does not exist!`。
	//
	// 必须排在 running 之前：英文无任务文案 `Collect task does not exist!` 里
	// 含 "collect"，先判 running 会把它归成"进行中"，平台就会一直等下去。
	case strings.Contains(status, "不存在"), strings.Contains(status, "无任务"),
		strings.Contains(status, "暂无"), strings.Contains(status, "没有正在"),
		strings.Contains(lower, "not exist"), strings.Contains(lower, "no task"),
		strings.Contains(lower, "none"):
		return NodeNone
	case strings.Contains(status, "进行中"), strings.Contains(status, "收集中"),
		strings.Contains(status, "上传中"), strings.Contains(status, "打包"), strings.Contains(status, "准备"),
		strings.Contains(lower, "progress"), strings.Contains(lower, "running"),
		strings.Contains(lower, "collect"), strings.Contains(lower, "upload"),
		strings.Contains(lower, "pack"):
		return NodeRunning
	}
	// 状态行缺失时用可观测信号兜底：有进度或有节点明细，说明任务存在且仍在推进。
	if progress.Percent > 0 || len(progress.Hosts) > 0 || progress.Done > 0 {
		return NodeRunning
	}
	return NodeUnknown
}

// classifyHostStatusWord 归一化节点明细中间列的状态词。
// 返回空串表示这一行不是节点明细——行首是 IP 的行也可能来自日志或命令回显，
// 不能见到 IP 就当成节点。
func ClassifyHostStatusWord(word string) string {
	w := strings.ToLower(strings.TrimSpace(word))
	switch {
	case strings.Contains(word, "失败"), strings.Contains(word, "异常"),
		strings.Contains(w, "fail"), strings.Contains(w, "error"),
		strings.Contains(w, "abnormal"):
		return HostFailed
	case strings.Contains(word, "完成"),
		strings.Contains(w, "success"), strings.Contains(w, "complete"),
		strings.Contains(w, "finish"), strings.Contains(w, "done"):
		return HostCompleted
	case strings.Contains(word, "收集"), strings.Contains(word, "进行"),
		strings.Contains(w, "collect"), strings.Contains(w, "progress"),
		strings.Contains(w, "running"), strings.Contains(w, "pack"),
		strings.Contains(w, "upload"), strings.Contains(w, "wait"):
		return HostRunning
	}
	return ""
}

// isFailureLine 判断上传块里的一行是否是失败原因。
// 不能只认中文「失败/错误/异常」：英文输出里是 "Failed"、"401 Unauthorized"。
func IsFailureLine(line string) bool {
	if strings.Contains(line, "失败") || strings.Contains(line, "错误") || strings.Contains(line, "异常") {
		return true
	}
	lower := strings.ToLower(line)
	return strings.Contains(lower, "fail") || strings.Contains(lower, "error") ||
		strings.Contains(lower, "unauthorized") || strings.Contains(lower, "timeout")
}

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*[A-Za-z]`)

// stripANSI 去掉终端转义序列：保留可读文本，避免解析与展示被颜色码污染。
func stripANSI(text string) string {
	return ansiRe.ReplaceAllString(text, "")
}

// truncate 截断到指定字节数并追加省略号（与 sshx.TruncateString 行为一致）。
func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}
