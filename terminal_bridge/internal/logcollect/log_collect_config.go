package logcollect

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
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"terminal_bridge/internal/sshx"

	"terminal_bridge/internal/shared/reconcile"
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
// S2 收口：写入段改由 reconcile 骨架驱动（InlineMessage Provider →
// remote.WriteFile 先校验再 mv → sha256/cmp 冒烟），本函数只保留产品语义：
// 路径按 task 隔离、schema 二次校验、以及调用方已在用的失败文案。
//
// 为什么不再自己拼 printf 直写：SSH 通道中断会留下**截断的配置**，
// 而 acli 对坏配置静默跳过（退出码仍 0、任务标 complete）——任务报成功、
// 数据却没采到。骨架的写入协议保证正式路径上永远只会是完整文件
// （校验失败只删 tmp），且降级档位显式上抛。
//
// 校验降级链保持不变：sha256sum 优先；HCI 宿主机是极简系统，可能没有该命令
// （stat 就没有，base64 也没有），此时降级为 printf 重传原文 + cmp 逐字节比对
// ——**不做"探测不了就跳过"**，否则一次写坏的配置会以"任务成功但少收"收场。
func writeLogCollectConfig(
	ctx context.Context,
	s *sshx.SSHSession,
	taskID, raw, expectedSHA string,
) (string, error) {
	path := logCollectConfigPath(taskID)
	if path == "" {
		return "", fmt.Errorf("任务 ID 非法，无法确定配置文件路径")
	}
	if err := verifyConfigSHA256(raw, expectedSHA); err != nil {
		return "", err
	}

	provider := reconcile.InlineMessage{
		// 前端是不可信链路：平台已校验过，桥仍须独立证明 schema 合法。
		Validate: func(text string) error {
			_, err := parseLogCollectConfig(text)
			return err
		},
		Smoke:   reconcile.SmokeSpec{Kind: reconcile.SmokeSHA256},
		Version: reconcile.VersionSpec{Mode: reconcile.VersionNone},
	}
	desired, err := provider.Desired(ctx, reconcile.SyncRequest{
		Kind:   reconcile.KindConfigFile,
		NodeIP: sshx.SessionHostAddress(s, ""),
		TaskID: taskID,
		Raw:    raw,
		SHA256: expectedSHA,
		Path:   path,
		Dir:    logCollectConfigDir,
		Mode:   "0644",
		Metadata: map[string]string{
			reconcile.MDKeyTaskID: taskID,
		},
	})
	if err != nil {
		return "", err
	}

	// 不落账本：config 按 task_id 隔离、总是覆写，账本对它不产生任何判定价值，
	// 逐任务落盘只会让 state 目录无界增长。
	res := reconcile.Run(ctx, reconcile.Deps{Runner: sshx.NewSSHRunner(s)}, desired[0])
	switch res.Status {
	case reconcile.StatusOK, reconcile.StatusSkipped:
		return path, nil
	}
	if res.Stage == reconcile.StageVerify {
		return "", fmt.Errorf("配置文件回读校验失败：%s", res.Detail)
	}
	return "", fmt.Errorf("写入配置文件失败：%s", res.Detail)
}

// preflightLogCollectPaths 逐条检查路径是否存在，返回缺失清单。
//
// 已知局限（必须在前端说清）：config 的 paths 是在**每台目标主机**按根 `/` 解析的，
// 而 bridge 只连发起节点，因此这里只证明"发起节点上有"。
// 探测命令本身跑不起来时不猜"都存在"，一律按全部缺失上报，交由用户二次确认。
func preflightLogCollectPaths(ctx context.Context, s *sshx.SSHSession, paths []string) []string {
	if len(paths) == 0 {
		return nil
	}
	quoted := make([]string, 0, len(paths))
	for _, path := range paths {
		quoted = append(quoted, ShellQuote(path))
	}
	command := "for p in " + strings.Join(quoted, " ") + `; do [ -e "$p" ] || echo "$p"; done`
	output, code, err := s.RunQuickCommand(ctx, command)
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
