package main

// spc 覆写：把平台场景写到每台目标节点的 spc/<slot>.json，并在任务终态恢复。
//
// 第一性原理：spc 由每台节点各自的 single_collect.py 现场读取，
// sfd_main 只下发脚本、不推送清单；只写发起节点会让其余节点仍按旧清单收集，
// 且没有任何报错（spc 缺失只 warning 跳过）。

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"
)

const (
	// 固定槽位：core 原清单已进 catalog，覆写后可勾回，损失最小。
	logCollectSpcSlot = "core"
	// 场景体上限：11 份官方清单最大约 6.5KB，512KB 足够且能挡住误传大文件。
	logCollectSpcMaxBytes = 512 * 1024
)

var (
	spcSlotRe   = regexp.MustCompile("^[a-z][a-z0-9_]{1,31}$")
	spcTaskIDRe = regexp.MustCompile("^[A-Za-z0-9._-]{1,64}$")
	spcDirRe    = regexp.MustCompile("^/[A-Za-z0-9_./-]+$")
)

// buildSpcFindCommand 探测 spc 目录：部署路径由 ABSOLUTE_DEPLOY_PATH 决定，
// 不同集群可能不同，硬编码等于赌某个版本的目录布局。
func buildSpcFindCommand() string {
	return "find /sf/data/local -maxdepth 6 -type d -name spc -path '*support_bundle_cluster*' 2>/dev/null"
}

// pickSpcDir 解析探测结果：0 个视为未安装，多个视为无法确定，都不猜。
func pickSpcDir(output string) (string, error) {
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
	switch len(dirs) {
	case 0:
		return "", fmt.Errorf("未找到 spc 目录（该节点可能未安装日志收集插件）")
	case 1:
		return dirs[0], nil
	default:
		return "", fmt.Errorf("找到多个 spc 目录，拒绝猜测：%s", strings.Join(dirs, ", "))
	}
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

// logCollectSpcHost 记录单台节点的写入结果：恢复靠它，不能只记一个总数。
type logCollectSpcHost struct {
	IP     string `json:"ip"`
	Backup string `json:"backup,omitempty"`
	Error  string `json:"error,omitempty"`
}

// logCollectSpcPlan 是 create 前必须完成的扇出结果。
type logCollectSpcPlan struct {
	Slot   string `json:"slot"`
	SHA256 string `json:"sha256"`
	Dir    string `json:"dir"`
	// LocalIP 不落 JSON：只用于区分本机直跑与 ssh 扇出。
	LocalIP string              `json:"-"`
	Hosts   []logCollectSpcHost `json:"hosts"`
}

// runSpcOnHost 在目标节点执行命令：本机直跑，其余走节点间裸 ssh 并显式加
// BatchMode/ConnectTimeout —— 否则 host key 未确认时后台命令会挂在 tty 等待。
func runSpcOnHost(ctx context.Context, s *SSHSession, host, localIP, command string) (string, int, error) {
	if host == "" || host == localIP {
		return s.runQuickCommand(ctx, command)
	}
	if !logCollectIPv4Re.MatchString(host) {
		return "", -1, fmt.Errorf("非法节点 IP：%s", host)
	}
	wrapped := fmt.Sprintf("ssh -o BatchMode=yes -o ConnectTimeout=5 %s %s", host, shellQuote(command))
	return s.runQuickCommand(ctx, wrapped)
}

// spcTargetOf 从备份路径还原目标文件：<target>.platform.<task_id>.bak
func spcTargetOf(backup string) string {
	if index := strings.Index(backup, ".platform."); index > 0 {
		return backup[:index]
	}
	return backup
}

// prepareLogCollectSpc 逐台备份并写入 spc，任一台失败即全部回滚。
//
// 必须先全部成功再 create：acli 只认节点本地文件，一台没写成功就会按旧清单
// 收集，而且不会报错。
func prepareLogCollectSpc(
	ctx context.Context,
	s *SSHSession,
	taskID, raw, expectedSHA, localIP string,
	hosts []string,
) (*logCollectSpcPlan, error) {
	if err := validateSpcContent(raw); err != nil {
		return nil, err
	}
	digest := spcSHA256(raw)
	if expectedSHA != "" && !strings.EqualFold(digest, strings.TrimSpace(expectedSHA)) {
		return nil, fmt.Errorf("场景摘要不一致（期望 %s，实际 %s）", expectedSHA, digest)
	}
	plan := &logCollectSpcPlan{Slot: logCollectSpcSlot, SHA256: digest, LocalIP: localIP}
	rollback := func() {
		for _, host := range plan.Hosts {
			if host.Backup == "" {
				continue
			}
			cmd := buildSpcRestoreCommand(host.Backup, spcTargetOf(host.Backup))
			_, _, _ = runSpcOnHost(ctx, s, host.IP, localIP, cmd)
		}
	}
	for _, host := range hosts {
		entry, dir, err := writeSpcOnHost(ctx, s, host, localIP, taskID, raw, digest)
		plan.Hosts = append(plan.Hosts, entry)
		if err != nil {
			plan.Hosts[len(plan.Hosts)-1].Error = err.Error()
			rollback()
			return nil, fmt.Errorf("节点 %s 写入失败：%w", host, err)
		}
		if plan.Dir == "" {
			plan.Dir = dir
		}
	}
	return plan, nil
}

// writeSpcOnHost 在单台节点完成「自愈 → 备份 → 写入 → 回读校验」。
func writeSpcOnHost(
	ctx context.Context,
	s *SSHSession,
	host, localIP, taskID, raw, digest string,
) (logCollectSpcHost, string, error) {
	entry := logCollectSpcHost{IP: host}
	out, code, err := runSpcOnHost(ctx, s, host, localIP, buildSpcFindCommand())
	if err != nil || code != 0 {
		return entry, "", fmt.Errorf("探测 spc 目录失败（exit=%d）：%v", code, err)
	}
	dir, err := pickSpcDir(out)
	if err != nil {
		return entry, "", err
	}
	target, err := spcFilePath(dir, logCollectSpcSlot)
	if err != nil {
		return entry, "", err
	}
	// 写前自愈：上一次没收尾的备份先恢复，避免把平台清单当原件备份。
	if listCmd, listErr := buildSpcBackupListCommand(dir, logCollectSpcSlot); listErr == nil {
		if backupOut, c, _ := runSpcOnHost(ctx, s, host, localIP, listCmd); c == 0 {
			if stale, pickErr := pickRestorableBackup(backupOut, target); pickErr == nil {
				_, _, _ = runSpcOnHost(ctx, s, host, localIP, buildSpcRestoreCommand(stale, target))
			}
		}
	}
	backup, err := spcBackupPath(target, taskID)
	if err != nil {
		return entry, "", err
	}
	if _, c, e := runSpcOnHost(ctx, s, host, localIP, buildSpcBackupCommand(target, backup)); e != nil || c != 0 {
		return entry, "", fmt.Errorf("备份失败（exit=%d）：%v", c, e)
	}
	entry.Backup = backup
	if _, c, e := runSpcOnHost(ctx, s, host, localIP, buildSpcWriteCommand(target, raw)); e != nil || c != 0 {
		return entry, "", fmt.Errorf("写入失败（exit=%d）：%v", c, e)
	}
	verifyCmd := fmt.Sprintf("sha256sum %s | awk '{print $1}'", shellQuote(target))
	got, c, e := runSpcOnHost(ctx, s, host, localIP, verifyCmd)
	if e != nil || c != 0 || !strings.EqualFold(strings.TrimSpace(got), digest) {
		return entry, "", fmt.Errorf("回读摘要不一致")
	}
	return entry, dir, nil
}

// resolveSpcHosts 解析目标节点：all 用集群在线列表，指定值用 IP 列表。
//
// spc 必须写到每一台目标节点；拿不到全集时拒绝，而不是猜一个子集。
func resolveSpcHosts(ctx context.Context, s *SSHSession, ips string) ([]string, error) {
	ips = strings.TrimSpace(ips)
	if ips == "" || ips == "all" {
		out, code, err := s.runCommandPty(ctx, logCollectAcliInvoke("--formatter json platform node list"), 8*time.Second)
		if err != nil || code != 0 {
			return nil, fmt.Errorf("无法获取集群在线节点列表（spc 必须写到每台目标节点）")
		}
		hosts := make([]string, 0, 8)
		for _, option := range parseLogCollectNodeList(out) {
			if option.IP != "" {
				hosts = append(hosts, option.IP)
			}
		}
		if len(hosts) == 0 {
			return nil, fmt.Errorf("在线节点列表为空")
		}
		return hosts, nil
	}
	hosts := make([]string, 0, 4)
	for _, part := range strings.Split(ips, ",") {
		ip := strings.TrimSpace(part)
		if ip == "" {
			continue
		}
		if !logCollectIPv4Re.MatchString(ip) {
			return nil, fmt.Errorf("非法节点 IP：%s", ip)
		}
		hosts = append(hosts, ip)
	}
	if len(hosts) == 0 {
		return nil, fmt.Errorf("目标节点为空")
	}
	return hosts, nil
}

// localIPv4 取发起节点 IP：判断哪台可以直跑、哪台需要 ssh。
func localIPv4(ctx context.Context, s *SSHSession) string {
	out, code, err := s.runQuickCommand(ctx, "hostname -I 2>/dev/null | awk '{print $1}'")
	if err != nil || code != 0 {
		return ""
	}
	return strings.TrimSpace(out)
}

const (
	// 恢复窗口：acli 采集可能跑很久（大专项单天约 4 分钟，多天更久）。
	logCollectSpcRestoreWindow   = 2 * time.Hour
	logCollectSpcRestoreInterval = 15 * time.Second
)

// awaitAndRestoreSpc 轮询进度，任务终态后逐台恢复原 core.json。
//
// 用独立 context：create 请求的 ctx 返回即取消，绑它会立刻丢掉恢复。
// 局限：bridge 进程被杀/会话断开时这个 goroutine 也会丢，
// 因此写前自愈（下一次写前先恢复残留备份）是必须的兜底。
func (s *SSHSession) awaitAndRestoreSpc(plan *logCollectSpcPlan, traceID, caseID, cui string) {
	if plan == nil || len(plan.Hosts) == 0 {
		return
	}
	ctx := context.Background()
	deadline := time.Now().Add(logCollectSpcRestoreWindow)
	for time.Now().Before(deadline) {
		out, code, err := s.runCommandPty(ctx, logCollectAcliInvoke("log_collect progress"), logCollectProgressTimeout)
		if err == nil && code == 0 {
			progress := parseLogCollectProgress(out)
			if progress.Percent >= 100 || strings.EqualFold(progress.Status, "complete") || strings.EqualFold(progress.Status, "failed") {
				break
			}
		}
		time.Sleep(logCollectSpcRestoreInterval)
	}
	restored := 0
	for _, host := range plan.Hosts {
		if host.Backup == "" {
			continue
		}
		cmd := buildSpcRestoreCommand(host.Backup, spcTargetOf(host.Backup))
		if _, code, err := runSpcOnHost(ctx, s, host.IP, plan.LocalIP, cmd); err == nil && code == 0 {
			restored++
		}
	}
	blog("INFO", "log_collect.spc_restored", "spc 场景恢复完成", traceID, caseID, "", cui, map[string]any{
		"slot": plan.Slot, "restored": restored, "total": len(plan.Hosts),
	})
}

// restoreLogCollectSpc 处理前端显式触发的恢复（双保险）。
func restoreLogCollectSpc(ctx context.Context, s *SSHSession, plan *logCollectSpcPlan, localIP string) error {
	var failures []string
	for _, host := range plan.Hosts {
		if host.Backup == "" {
			continue
		}
		cmd := buildSpcRestoreCommand(host.Backup, spcTargetOf(host.Backup))
		if _, code, err := runSpcOnHost(ctx, s, host.IP, localIP, cmd); err != nil || code != 0 {
			failures = append(failures, host.IP)
		}
	}
	if len(failures) > 0 {
		return fmt.Errorf("恢复失败的节点：%s", strings.Join(failures, ","))
	}
	return nil
}
