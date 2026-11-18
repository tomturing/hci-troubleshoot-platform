package remote

import (
	"context"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// probe.go —— 远端探测：存在性 / 大小 / 摘要 / 版本（收口设计 §4.2）。
//
// 降级策略与写入侧同源：sha256sum 不存在 → md5sum → size-only，
// 结果显式标注档位。探测不到不等于"不存在"，也不等于"存在"——
// 命令跑不起来时返回 error，由调用方决定降级口径，原语不替它猜。

// ProbeMissingMark 表示节点上不存在可用文件（不存在或 0 字节）。
const ProbeMissingMark = "__ACLI_PROBE_MISSING"

// ProbeResult 是一次文件探测的事实。
type ProbeResult struct {
	Size    int64
	Algo    string // sha256 | md5 | size_only
	Digest  string
	Missing bool // 不存在或空文件
	Raw     string
}

// BuildProbeCommand 只读地报告远端文件的 size/digest。
//
// 为什么重试安装前必须先探测：直接重跑安装命令等于无条件信任盘上那份文件，
// 而上一轮正是因为"盘上那份包不对"才失败的。探测让"复用"变成可验证的决定。
func BuildProbeCommand(path string) string {
	quoted := ShellQuote(path)
	return "if [ ! -s " + quoted + " ]; then echo \"" + ProbeMissingMark + "\"; else " +
		`SIZE=$(wc -c < ` + quoted + ` 2>/dev/null | tr -d ' \r\n'); ALGO=size_only; DIG=""; ` +
		`if command -v sha256sum >/dev/null 2>&1; then ALGO=sha256; DIG=$(sha256sum ` + quoted + ` 2>/dev/null | awk '{print $1}'); ` +
		`elif command -v md5sum >/dev/null 2>&1; then ALGO=md5; DIG=$(md5sum ` + quoted + ` 2>/dev/null | awk '{print $1}'); fi; ` +
		`echo "size=${SIZE} algo=${ALGO} digest=${DIG}"; fi`
}

// ParseProbeOutput 解析探测结果；Missing 为真表示节点上没有可用文件。
func ParseProbeOutput(output string) ProbeResult {
	clean := StripANSI(output)
	result := ProbeResult{Raw: strings.TrimSpace(output)}
	if strings.Contains(clean, ProbeMissingMark) {
		result.Missing = true
		return result
	}
	lines := strings.Split(clean, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if match := writeLineRe.FindStringSubmatch(strings.TrimSpace(lines[i])); match != nil {
			parsed, _ := strconv.ParseInt(match[1], 10, 64)
			result.Size = parsed
			result.Algo = match[2]
			result.Digest = strings.ToLower(match[3])
			return result
		}
	}
	return result
}

// ProbeFile 探测远端文件的存在性、大小、摘要。
//
// 命令本身跑不起来时返回 error（不猜"都存在"也不猜"都不存在"）：
// 探测失败与探测到缺失是两种结论，混为一谈会让重试逻辑走错分支。
func ProbeFile(ctx context.Context, r Runner, path string) (ProbeResult, error) {
	probeCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	res := Run(probeCtx, r, BuildProbeCommand(path))
	if res.Err != nil {
		return ProbeResult{}, res.Err
	}
	return ParseProbeOutput(res.Stdout), nil
}

// VersionInfo 是远端已安装软件的版本事实。
type VersionInfo struct {
	Version   string
	BuildTime string
	Binary    string // 实际命中的可执行入口路径
	Absent    bool   // 节点上根本没有这个软件
	Raw       string
}

var versionLineRe = regexp.MustCompile(`^\d+\.\d+(\.\d+)?`)

// ParseVersionOutput 解析版本探测输出。
//
// 契约：`build <时间>` 行给构建时间；首个形如 x.y[.z] 的行给版本；
// `__ACLI_BIN__=<path>` 给实际入口；`__ACLI_ABSENT__` 表示未安装。
func ParseVersionOutput(output string) VersionInfo {
	info := VersionInfo{Raw: output}
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		line = strings.TrimSpace(line)
		switch {
		case line == "__ACLI_ABSENT__":
			info.Absent = true
		case strings.HasPrefix(line, "__ACLI_BIN__="):
			info.Binary = strings.TrimSpace(strings.TrimPrefix(line, "__ACLI_BIN__="))
		case strings.HasPrefix(line, "build "):
			info.BuildTime = strings.TrimSpace(strings.TrimPrefix(line, "build "))
		default:
			if info.Version == "" && versionLineRe.MatchString(line) {
				info.Version = line
			}
		}
	}
	return info
}

// ProbeVersion 探测远端已安装软件的版本。
//
// 为什么参数是命令而不是 kind 字符串：acli 的版本探测命令带 PATH 前置与
// 自解析（非交互 shell 的 PATH 不含 /sf/bin），那是 log_collect 链路的既有
// 契约，搬进通用原语会让 remote 认识具体产品。kind→命令的映射留在调用方，
// 原语只负责"执行 + 解析 + 如实标注未安装"。
func ProbeVersion(ctx context.Context, r Runner, command string) (VersionInfo, error) {
	res := Run(ctx, r, command)
	if res.Err != nil {
		return VersionInfo{}, res.Err
	}
	return ParseVersionOutput(res.Stdout), nil
}
