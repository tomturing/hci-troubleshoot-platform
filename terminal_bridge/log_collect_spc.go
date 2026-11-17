package main

// spc 覆写：把平台场景写到每台目标节点的 spc/<slot>.json，并在任务终态恢复。
//
// 第一性原理：spc 由每台节点各自的 single_collect.py 现场读取，
// sfd_main 只下发脚本、不推送清单；只写发起节点会让其余节点仍按旧清单收集，
// 且没有任何报错（spc 缺失只 warning 跳过）。

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

const (
	// 固定槽位：core 原清单已进 catalog，覆写后可勾回，损失最小。
	logCollectSpcSlot = "core"
	// 场景体上限：11 份官方清单最大约 6.5KB，512KB 足够且能挡住误传大文件。
	logCollectSpcMaxBytes = 512 * 1024
	// spcDeployDir 是日志收集唯一使用的场景清单目录：acli 插件自部署，平台负责写入/恢复。
	// 2026-11-17 现场确认节点上另有 aDeploy 下的同名 spc 目录，属正常现象，日志收集不关心。
	// 固定路径由平台自己部署，探测（find）只增加误判面，不产生任何新信息，故直接使用。
	spcDeployDir = "/sf/data/local/acli/plugins/log_collect/tools/lmt_collect_info/support_bundle_cluster/spc"
)

var (
	spcSlotRe   = regexp.MustCompile("^[a-z][a-z0-9_]{1,31}$")
	spcTaskIDRe = regexp.MustCompile("^[A-Za-z0-9._-]{1,64}$")
	spcDirRe    = regexp.MustCompile("^/[A-Za-z0-9_./-]+$")
)

// buildSpcDirProbe 输出固定路径存在性检查脚本：存在则回显路径，不存在则无输出。
// 2026-11-17：目录由 acli 插件自部署，日志收集固定使用 spcDeployDir，
// 不再用 find 探测（多目录 + exit 1 曾导致 LC-SPC-001 误报）。
// 缺失即创建：acli 重装会清掉插件目录（含 spc），下次采集写清单前必须能把
// 被清的固定目录重建出来，否则 spc_dir_missing 会挡住自定义/默认场景下发。
// 该路径是平台自有的固定目录，mkdir -p 安全且不引入误判面。
func buildSpcDirProbe() string {
	q := shellQuote(spcDeployDir)
	return fmt.Sprintf("[ -d %s ] || mkdir -p %s; [ -d %s ] && printf '%%s\\n' %s", q, q, q, q)
}

// buildSpcFindCommand 保留旧函数名仅为兼容存量测试，语义已改为固定路径检查。
func buildSpcFindCommand() string {
	return buildSpcDirProbe()
}

// parseSpcDirs 归一化目录探测输出：去空行、去重、只留绝对路径、排序。
func parseSpcDirs(output string) []string {
	dirs := make([]string, 0, 2)
	seen := map[string]bool{}
	for _, line := range strings.Split(output, "\n") {
		dir := strings.TrimSpace(strings.ReplaceAll(line, "\r", ""))
		if dir == "" || !strings.HasPrefix(dir, "/") || strings.Contains(dir, "..") || seen[dir] {
			continue
		}
		seen[dir] = true
		dirs = append(dirs, dir)
	}
	sort.Strings(dirs)
	return dirs
}

// pickSpcDir 解析探测结果：只认固定路径 spcDeployDir，其余一律视为不存在。
// 现场 2026-11-17 确认节点上另有 aDeploy 下同名 spc 目录属正常现象，日志收集不关心，
// 因此不再做"多目录择一"判断——固定路径命中即用，未命中即缺失。
func pickSpcDir(output string) (string, error) {
	for _, dir := range parseSpcDirs(output) {
		if dir == spcDeployDir {
			return dir, nil
		}
	}
	return "", fmt.Errorf("固定 spc 目录不存在（%s），该节点可能未安装日志收集插件", spcDeployDir)
}

// spcResolveDir 是 pickSpcDir 的"错误码版"：扇出逐台失败要按稳定码归类。
// 固定路径方案下不再产生 spc_dir_ambiguous：命中固定路径即成功，否则一律 spc_dir_missing。
func spcResolveDir(output string) (string, string) {
	dir, err := pickSpcDir(output)
	if err != nil {
		return "", spcErrDirMissing
	}
	return dir, ""
}

// spcFilePath 拼出目标文件路径；槽位会进 shell 命令，必须先过白名单。
func spcFilePath(dir, slot string) (string, error) {
	if !strings.HasPrefix(dir, "/") || strings.Contains(dir, "..") {
		return "", fmt.Errorf("spc 目录非法：%s", dir)
	}
	if !spcSlotRe.MatchString(slot) {
		return "", fmt.Errorf("spc 槽位非法：%s", slot)
	}
	return dir + "/" + slot + ".json", nil
}

// spcBackupPath 备份必须按任务隔离：固定文件名会让两次采集互相覆盖。
func spcBackupPath(path, taskID string) (string, error) {
	if !spcTaskIDRe.MatchString(strings.TrimSpace(taskID)) {
		return "", fmt.Errorf("任务 ID 非法，无法确定备份路径")
	}
	return path + ".platform." + taskID + ".bak", nil
}

func spcSHA256(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// validateSpcContent 二级校验平台下发的 spc 文本。
//
// 平台侧已经按 schema 校验过，但原文经前端透传（不可信链路），
// bridge 必须独立证明：是数组、每条 action 合法、path 非空且不含换行。
func validateSpcContent(raw string) error {
	if len(strings.TrimSpace(raw)) == 0 {
		return fmt.Errorf("场景内容为空")
	}
	if len(raw) > logCollectSpcMaxBytes {
		return fmt.Errorf("场景过大：%d 字节（上限 %d）", len(raw), logCollectSpcMaxBytes)
	}
	var items []map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &items); err != nil {
		return fmt.Errorf("场景不是合法 JSON 数组：%w", err)
	}
	if len(items) == 0 {
		return fmt.Errorf("场景至少需要一条收集项")
	}
	actions := map[string]bool{"copy": true, "command": true, "script": true}
	for index, item := range items {
		if _, ok := item["note"]; ok && len(item) == 1 {
			continue
		}
		var action string
		if err := json.Unmarshal(item["action"], &action); err != nil || !actions[action] {
			return fmt.Errorf("第 %d 条 action 非法", index+1)
		}
		var path string
		if err := json.Unmarshal(item["path"], &path); err != nil || strings.TrimSpace(path) == "" {
			return fmt.Errorf("第 %d 条 path 为空", index+1)
		}
		if strings.ContainsAny(path, "\r\n") {
			return fmt.Errorf("第 %d 条 path 含换行", index+1)
		}
	}
	return nil
}

// buildSpcWriteCommand 用 base64 写入：JSON 里的引号与 $ 在 heredoc 中会被展开，
// 直写等价于把场景内容当脚本片段执行。
func buildSpcWriteCommand(path, raw string) string {
	encoded := base64.StdEncoding.EncodeToString([]byte(raw))
	return fmt.Sprintf("printf '%%s' %s | base64 -d > %s", shellQuote(encoded), shellQuote(path))
}

func buildSpcBackupCommand(path, backup string) string {
	return fmt.Sprintf("cp -p %s %s", shellQuote(path), shellQuote(backup))
}

func buildSpcRestoreCommand(backup, target string) string {
	return fmt.Sprintf("cp -p %s %s", shellQuote(backup), shellQuote(target))
}

// buildSpcBackupListCommand 按修改时间倒序列出平台留下的备份。
//
// 写前先恢复是整条链路的关键兜底：终态恢复依赖 bridge 进程存活，
// 进程重启/断连会把平台清单永久留在节点上；下一次写之前先把它恢复掉，
// 比等人工发现可靠得多。
func buildSpcBackupListCommand(dir, slot string) (string, error) {
	if !spcDirRe.MatchString(dir) || strings.Contains(dir, "..") {
		return "", fmt.Errorf("spc 目录非法：%s", dir)
	}
	if !spcSlotRe.MatchString(slot) {
		return "", fmt.Errorf("spc 槽位非法：%s", slot)
	}
	// 不加引号：需要 shell 展开 glob；目录与槽位均已过白名单，不存在注入面。
	return fmt.Sprintf("ls -1t %s/%s.json.platform.*.bak 2>/dev/null", dir, slot), nil
}

// pickRestorableBackup 从 ls -1t 的输出里取第一个合法备份。
//
// 输出已按 mtime 倒序，因此第一个就是最近一次写入前的原件。
func pickRestorableBackup(output, target string) (string, error) {
	prefix := target + ".platform."
	for _, line := range strings.Split(output, "\n") {
		path := strings.TrimSpace(strings.ReplaceAll(line, "\r", ""))
		if !strings.HasPrefix(path, prefix) || !strings.HasSuffix(path, ".bak") {
			continue
		}
		if strings.Contains(path, "..") {
			continue
		}
		return path, nil
	}
	return "", fmt.Errorf("没有可恢复的备份")
}
