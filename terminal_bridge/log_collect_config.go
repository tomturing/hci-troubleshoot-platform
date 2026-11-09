package main

// 自定义场景（acli log_collect -c）的节点侧落地。
//
// 第一性原理：
//  1. -c 要求节点磁盘上存在一个 JSON 文件，平台唯一能写节点文件的通道是本 SSH 会话；
//  2. 配置原文由平台生成、经前端透传，**前端是不可信链路**，bridge 必须二次校验；
//  3. acli 对配置解析失败/路径不存在都是**静默跳过**（退出码仍 0、任务标 complete），
//     所以"写没写对、路径在不在"只能由 bridge 在下发前显式证明。

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

const (
	// 与 acli 产物同盘：/sf/data/local 在受限 shell 里可写，且采集进程可见。
	logCollectConfigDir = "/sf/data/local/acli/tmp/platform/log_collect"
	// 配置体上限：与 admin 保存前试算的阈值一致。
	logCollectConfigMaxBytes = 256 * 1024
	// 单份配置允许的最大路径条数（展开 {date} 后仍受此约束）。
	logCollectConfigMaxPaths = 200
)

// configTaskIDRe 限定 task_id 字符集：它会进文件路径，而路径会进 shell 命令。
var configTaskIDRe = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// logCollectConfigPath 返回固定目录下的按任务隔离路径。
//
// 按 task_id 命名是硬要求：插件不删配置文件，同一个固定文件名会让
// 下一次采集读到上一次的内容（或反过来覆盖还没用的配置）。
func logCollectConfigPath(taskID string) string {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" || !configTaskIDRe.MatchString(taskID) {
		return ""
	}
	path := logCollectConfigDir + "/cfg-" + taskID + ".json"
	if !logCollectPathRe.MatchString(path) {
		return ""
	}
	return path
}

// parseLogCollectConfig 校验平台下发的配置原文，返回去重后的字面路径。
//
// 为什么严格：这些路径会被逐条拼进 `cp -r`（插件未加引号），
// 一个含分号或空格的路径等价于在客户节点执行任意命令。
// 为什么只允许一个键：acli 的 config 还有 modules 等字段，但它们因
// `-m default="other"` 永不生效；写进去只会让人误以为"专项被覆盖了"。
func parseLogCollectConfig(raw string) ([]string, error) {
	if len(raw) == 0 {
		return nil, fmt.Errorf("配置内容为空")
	}
	if len(raw) > logCollectConfigMaxBytes {
		return nil, fmt.Errorf("配置过大：%d 字节（上限 %d）", len(raw), logCollectConfigMaxBytes)
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &fields); err != nil {
		return nil, fmt.Errorf("配置不是合法 JSON：%w", err)
	}
	for key := range fields {
		if key != "paths" {
			return nil, fmt.Errorf("配置含不允许的字段 %q（acli 会静默忽略它）", key)
		}
	}

	var body struct {
		Paths string `json:"paths"`
	}
	if err := json.Unmarshal([]byte(raw), &body); err != nil {
		return nil, fmt.Errorf("paths 字段非法：%w", err)
	}

	seen := make(map[string]bool)
	paths := make([]string, 0, 16)
	for _, candidate := range strings.Split(body.Paths, ",") {
		path := strings.TrimSpace(candidate)
		if path == "" {
			continue
		}
		if !logCollectPathRe.MatchString(path) || strings.Contains(path, "..") {
			return nil, fmt.Errorf("非法路径：%s", path)
		}
		if seen[path] {
			continue
		}
		seen[path] = true
		paths = append(paths, path)
	}
	if len(paths) > logCollectConfigMaxPaths {
		return nil, fmt.Errorf("路径条数 %d 超过上限 %d", len(paths), logCollectConfigMaxPaths)
	}
	return paths, nil
}

// verifyConfigSHA256 独立计算摘要，便于单测与调用方复用。
func verifyConfigSHA256(raw, expected string) error {
	sum := sha256.Sum256([]byte(raw))
	got := hex.EncodeToString(sum[:])
	if expected == "" || !strings.EqualFold(got, strings.TrimSpace(expected)) {
		return fmt.Errorf("配置摘要不一致（期望 %s，实际 %s）", expected, got)
	}
	return nil
}

// writeLogCollectConfig 把配置写入发起节点并证明"写下去的就是平台签发的内容"。
//
// 写入用 base64 解码而不是 heredoc：JSON 里的引号、`$`、反引号在 shell 里
// 会被展开，heredoc 直写等价于把配置内容当成脚本片段执行。
//
// 校验优先 sha256sum；受限 shell 可能没有该命令（stat 就没有），
// 此时降级为 base64 回读原文逐字节比对——**不做"探测不了就跳过"**，
// 否则一次写坏的配置会以"任务成功但少收"收场。
func writeLogCollectConfig(
	ctx context.Context,
	s *SSHSession,
	taskID, raw, expectedSHA string,
) (string, error) {
	path := logCollectConfigPath(taskID)
	if path == "" {
		return "", fmt.Errorf("任务 ID 非法，无法确定配置文件路径")
	}
	if err := verifyConfigSHA256(raw, expectedSHA); err != nil {
		return "", err
	}

	encoded := base64.StdEncoding.EncodeToString([]byte(raw))
	writeCmd := fmt.Sprintf(
		`mkdir -p %s && printf '%%s' %s | base64 -d > %s`,
		shellQuote(logCollectConfigDir), shellQuote(encoded), shellQuote(path),
	)
	if _, code, err := s.runQuickCommand(ctx, writeCmd); err != nil || code != 0 {
		return "", fmt.Errorf("写入配置文件失败（exit=%d）：%v", code, err)
	}

	digestCmd := fmt.Sprintf(`sha256sum %s 2>/dev/null | awk '{print $1}'`, shellQuote(path))
	digestOut, code, digestErr := s.runQuickCommand(ctx, digestCmd)
	if digestErr == nil && code == 0 && strings.EqualFold(strings.TrimSpace(digestOut), expectedSHA) {
		return path, nil
	}

	// 降级：回读原文。base64 输出可能带换行，统一剥掉再比。
	readCmd := fmt.Sprintf(`base64 %s | tr -d '\n'`, shellQuote(path))
	readOut, code, err := s.runQuickCommand(ctx, readCmd)
	onDisk := strings.NewReplacer("\r", "", "\n", "", " ", "").Replace(readOut)
	if err != nil || code != 0 {
		return "", fmt.Errorf("配置文件回读失败（exit=%d）：%v", code, err)
	}
	if onDisk != encoded {
		return "", fmt.Errorf("配置文件内容与平台签发内容不一致")
	}
	return path, nil
}

// preflightLogCollectPaths 逐条检查路径是否存在，返回缺失清单。
//
// 已知局限（必须在前端说清）：config 的 paths 是在**每台目标主机**按根 `/` 解析的，
// 而 bridge 只连发起节点，因此这里只证明"发起节点上有"。
// 探测命令本身跑不起来时不猜"都存在"，一律按全部缺失上报，交由用户二次确认。
func preflightLogCollectPaths(ctx context.Context, s *SSHSession, paths []string) []string {
	if len(paths) == 0 {
		return nil
	}
	quoted := make([]string, 0, len(paths))
	for _, path := range paths {
		quoted = append(quoted, shellQuote(path))
	}
	command := "for p in " + strings.Join(quoted, " ") + `; do [ -e "$p" ] || echo "$p"; done`
	output, code, err := s.runQuickCommand(ctx, command)
	if err != nil || code != 0 {
		return append([]string(nil), paths...)
	}

	missing := make([]string, 0)
	known := make(map[string]bool, len(paths))
	for _, path := range paths {
		known[path] = true
	}
	for _, line := range strings.Split(output, "\n") {
		value := strings.TrimSpace(strings.ReplaceAll(line, "\r", ""))
		if known[value] {
			missing = append(missing, value)
		}
	}
	return missing
}
