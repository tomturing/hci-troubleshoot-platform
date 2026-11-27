package logcollect

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"sync"

	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/remote"
)

// 归档路径由 acli 固定为 `{log_collect 目录}/{工单号}.zip`：同一工单重新采集必然
// 同名覆盖。因此"路径上有个包"与"这个包是本次采集产出的"是两件事，只看体积会
// 把上一批次残留的包当成本次产物（Q2026112700769：取消 vm_suspend 后改收 slowdisk，
// 旧包 4.17 GiB 在固定路径上压了 14 分钟，被进度探测当成本次 archive_size 上报）。
//
// 第一性原理：判断"是不是本次写的"，最底层的证据是文件身份（size + mtime + inode）
// 在任务开始前后有没有变化。create 之前先量一次，之后每次探测再量，完全一致就说明
// 这个 inode 从任务开始到现在没被写过——它不是本次采集的产物。
//
// 为什么必须由桥侧记录而不是平台：只有桥在任务下发的那一刻站在这台节点的文件系统前，
// 平台的任何时刻线（任务创建时间、上报时间）都比文件 mtime 粗，且节点与平台未必对时。
type archiveBaseline struct {
	Path    string
	Size    int64
	ModUnix int64
	Inode   uint64
}

// archiveBaselineRunner 只依赖"跑一条只读命令"的能力，便于在没有 SSH 的测试里穷举判定。
type archiveBaselineRunner interface {
	RunQuickCommand(ctx context.Context, cmd string) (string, int, error)
}

var (
	// logCollectArchiveBaselines 是任务级快照：桥是长驻进程，一个进程可能连续下发
	// 多个任务，按 ExecID 隔开才能保证"这个基线属于这一次下发"。
	// 刻意不做定期清理：条目只有一个结构体大小，而基线一旦丢失就会重新把残留包
	// 当成本次产物；进程重启后基线消失是安全方向（退回原来的行为，不做错误抑制）。
	logCollectArchiveBaselines sync.Map // execID -> archiveBaseline
	// logCollectArchiveResidualNotified 保证同一任务只落一条残留告警：
	// 进度每 5 秒一轮，逐轮打日志会把真正的现场信号淹掉。
	logCollectArchiveResidualNotified sync.Map
)

// logCollectArchiveStatCommand 构造一次取全量文件身份的只读命令。
//
// 为什么用 `stat -c '%s|%Y|%i'` 而不是分别调 stat/ls：`ls -l` 给不出 inode 与秒级
// mtime，分开调又要三条命令；一条命令取全，失败即放弃（不猜）。
func logCollectArchiveStatCommand(path string) string {
	return fmt.Sprintf("stat -c '%%s|%%Y|%%i' %s", remote.ShellQuote(path))
}

// parseArchiveStatTriple 解析 `size|mtime|inode`。
//
// 只认严格三段数字：受限 shell 里 stat 不认格式串时会原样打印 "%s|%Y|%i" 或只给
// 一行报错，任何"尽力而为"的解析都可能把垃圾当体积，宁可判定解析失败。
func parseArchiveStatTriple(output string) (size int64, modUnix int64, inode uint64, ok bool) {
	fields := strings.Split(strings.TrimSpace(output), "|")
	if len(fields) != 3 {
		return 0, 0, 0, false
	}
	parsedSize, err := strconv.ParseInt(strings.TrimSpace(fields[0]), 10, 64)
	if err != nil || parsedSize < 0 {
		return 0, 0, 0, false
	}
	parsedMod, err := strconv.ParseInt(strings.TrimSpace(fields[1]), 10, 64)
	if err != nil || parsedMod <= 0 {
		return 0, 0, 0, false
	}
	parsedInode, err := strconv.ParseUint(strings.TrimSpace(fields[2]), 10, 64)
	if err != nil || parsedInode == 0 {
		return 0, 0, 0, false
	}
	return parsedSize, parsedMod, parsedInode, true
}

// readArchiveBaseline 量一次文件身份；读不到（不存在/命令不可用/格式不支持）返回 false。
func readArchiveBaseline(ctx context.Context, r archiveBaselineRunner, path string) (archiveBaseline, bool) {
	if r == nil || strings.TrimSpace(path) == "" {
		return archiveBaseline{}, false
	}
	output, exitCode, err := r.RunQuickCommand(ctx, logCollectArchiveStatCommand(path))
	if err != nil || exitCode != 0 {
		return archiveBaseline{}, false
	}
	size, modUnix, inode, ok := parseArchiveStatTriple(output)
	if !ok {
		return archiveBaseline{}, false
	}
	return archiveBaseline{Path: strings.TrimSpace(path), Size: size, ModUnix: modUnix, Inode: inode}, true
}

// rememberLogCollectArchiveBaseline 在 create 之前记录固定路径上已有文件的身份。
//
// 调用时序要求：必须在 archiveStaleLogCollectArchive 之后、acli create 之前——
// 旧包能改名成功时基线应为"不存在"，改名失败时基线正好覆盖那份残留。
func rememberLogCollectArchiveBaseline(
	ctx context.Context,
	r archiveBaselineRunner,
	execID, caseID, nodeIP, traceID, cui string,
) {
	execID = strings.TrimSpace(execID)
	if execID == "" {
		return
	}
	path := fallbackLogCollectArchivePath(caseID)
	if path == "" {
		return
	}
	base, ok := readArchiveBaseline(ctx, r, path)
	if !ok {
		// 没有残留文件是常态：不记基线，也不打日志（每个任务一条会变成噪音）。
		return
	}
	logCollectArchiveBaselines.Store(execID, base)
	// 这条事件回答的正是现场最难回答的问题："下发之前盘上是不是已经有包"。
	evidence.Emit("INFO", "log_collect.archive_baseline", "采集前已存在同名压缩包", traceID, caseID, nodeIP, cui, map[string]any{
		"task_id": execID,
		"path":    base.Path,
		"size":    base.Size,
		"mtime":   base.ModUnix,
		"inode":   base.Inode,
	})
}

// logCollectArchiveIsResidual 判断探测到的包是否就是任务开始前那一份（未被写过）。
//
// 保守原则：只有"能证明完全一致"才判残留。任何一次量不到（SSH 抖动、命令被限）
// 都返回 false，让体积照常上报——宁可多报一次，也不能把本次真正的产物静默掉。
func logCollectArchiveIsResidual(
	ctx context.Context,
	r archiveBaselineRunner,
	execID, rawPath string,
	size int64,
) bool {
	execID = strings.TrimSpace(execID)
	if execID == "" || size <= 0 {
		return false
	}
	stored, ok := logCollectArchiveBaselines.Load(execID)
	if !ok {
		return false
	}
	base, ok := stored.(archiveBaseline)
	if !ok {
		return false
	}
	if strings.TrimSpace(rawPath) != base.Path || size != base.Size {
		return false
	}
	current, ok := readArchiveBaseline(ctx, r, rawPath)
	if !ok {
		return false
	}
	if current.Size != base.Size || current.ModUnix != base.ModUnix || current.Inode != base.Inode {
		// 体积可能碰巧相同，但只要 mtime/inode 变了，就是本次重写过的文件。
		return false
	}
	return true
}

// markArchiveResidualNotified 保证同一任务只落一条残留告警。
func markArchiveResidualNotified(execID string) bool {
	if strings.TrimSpace(execID) == "" {
		return false
	}
	_, loaded := logCollectArchiveResidualNotified.LoadOrStore(execID, true)
	return !loaded
}
