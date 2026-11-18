package reconcile

// activate.go —— 三钩子的执行器与构造性回滚不变量（设计 §2.4.1）。
//
// B1 是 P0：旧 rollback() 在备份不存在时仍 rm -rf 现场插件目录，而 lock_timeout /
// register_backup_failed 都发生在目录备份之前——一次锁竞争超时就能删掉现场唯一副本。
// 修它不能靠"记得先判断备份"，要靠结构：删除动作只可能出现在
// 「已登记写入集 ∧ 备份名非空 ∧ 节点上备份确实存在」三个前提之后。
// 本文件把这三个前提写成代码路径，任何绕过它们的调用都拿不到删除命令。
//
// 备份名必须由调用方按 kind 给定（任务/部署隔离）：
//
//	spc:    <path>.platform.<taskID>.bak
//	plugin: <path>.old.<syncID> 与 $TMP/backup/<name>
//
// 固定 .bak 后缀会让两次采集互相覆盖原件（log_collect_spc.go 的硬要求）。

import (
	"context"
	"fmt"
	"sort"
	"sync"

	"terminal_bridge/internal/shared/remote"
)

// 还原脚本的退出码（与写入协议同一套编号风格，便于现场对号）：
//
//	20 = 复制失败	21 = 备份不存在	22 = 还原后逐字节校验不符	23 = 挪开现场失败
const (
	restoreExitCopyFailed   = 20
	restoreExitNoBackup     = 21
	restoreExitVerifyFailed = 22
	restoreExitMoveFailed   = 23
)

// ErrNoBackup 表示"节点上没有可还原的备份"。
//
// 它是 Restore 的正常分支（幂等重放：备份已被上一次还原消费掉），
// 不是失败终态；Rollback 遇到同一事实则必须报错——失败回滚时没有备份
// 意味着现场无法恢复，绝不能假装撤销成功。
var ErrNoBackup = fmt.Errorf("没有可恢复的备份")

// WriteEntry 登记一次实际写入。文件与目录树均可登记：
// 插件回滚是目录树粒度，只登记文件会漏掉它。
type WriteEntry struct {
	Path       string
	IsDirTree  bool
	BackupPath string
}

// Activator 持有本次部署的写入集，并提供回滚/还原。
// 零值可用；nil 接收者安全（表示"没有任何登记"，因此什么都不会被动）。
type Activator struct {
	mu         sync.Mutex
	writtenSet map[string]WriteEntry
}

// NewActivator 建一个空写入集。
func NewActivator() *Activator { return &Activator{} }

// Register 登记写入成功的条目。同路径重复登记以最后一次为准
// （重试写入会换备份名，旧备份名不该再被当成前提）。
func (a *Activator) Register(entries ...WriteEntry) {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.writtenSet == nil {
		a.writtenSet = map[string]WriteEntry{}
	}
	for _, entry := range entries {
		if entry.Path == "" {
			continue
		}
		a.writtenSet[entry.Path] = entry
	}
}

// Registered 查某路径是否登记过。
func (a *Activator) Registered(path string) (WriteEntry, bool) {
	if a == nil {
		return WriteEntry{}, false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	entry, ok := a.writtenSet[path]
	return entry, ok
}

// Entries 返回全部登记条目（按路径排序，便于日志与回执比对）。
func (a *Activator) Entries() []WriteEntry {
	if a == nil {
		return nil
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]WriteEntry, 0, len(a.writtenSet))
	for _, entry := range a.writtenSet {
		out = append(out, entry)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}

// AssertRestorable 是构造性不变量的**判定部分**，供自带设备脚本的消费者复用
// （spc 的还原脚本已在 remote/marker.go 登记，不该为了收口而换掉它的回执标记）。
//
// 规则：未登记的条目直接剔除（从未写入的节点没有需要撤销的改动）；
// 登记了但备份名为空的条目一律拒绝，并返回 error 而不是跳过——
// 跳过会让调用方以为"回滚已完成"，而现场其实还留着平台内容。
// 返回的条目以**登记值**为准：调用方传空备份名不能绕过不变量。
func (a *Activator) AssertRestorable(entries []WriteEntry) ([]WriteEntry, error) {
	if a == nil {
		return nil, nil
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]WriteEntry, 0, len(entries))
	for _, entry := range entries {
		registered, ok := a.writtenSet[entry.Path]
		if !ok {
			continue
		}
		if registered.BackupPath == "" {
			return nil, fmt.Errorf("回滚前提不满足：%s 未登记备份，拒绝删除", entry.Path)
		}
		out = append(out, registered)
	}
	return out, nil
}

// RestoreOutcome 是一条还原结论。State 取值 restored / skipped / failed。
type RestoreOutcome struct {
	Path   string
	State  string
	Detail string
}

// Rollback 撤销本次写入：只动登记过的条目，且备份必须存在。
// 备份缺失或探测失败一律返回 error，绝不删除（B1）。
func (a *Activator) Rollback(ctx context.Context, r remote.Runner, entries []WriteEntry) error {
	allowed, err := a.AssertRestorable(entries)
	if err != nil {
		return err
	}
	if r == nil {
		return fmt.Errorf("缺少远端执行器，无法回滚")
	}
	for _, entry := range allowed {
		outcome := restoreEntry(ctx, r, entry)
		switch outcome.State {
		case "restored":
			continue
		case "skipped":
			return fmt.Errorf("%w：%s（备份 %s）", ErrNoBackup, entry.Path, entry.BackupPath)
		default:
			return fmt.Errorf("回滚 %s 失败：%s", entry.Path, outcome.Detail)
		}
	}
	return nil
}

// Restore 把原件放回去（spc 终态还原 / 写前自愈 / 失败回滚的宽松版）。
//
// 与 Rollback 的唯一语义差异：备份不存在记为 skipped 而不是失败。
// 幂等重放不能把"没有备份"说成"还原失败"，否则每次重试都推导出
// "可能残留平台清单"的假告警。其余前提（登记、备份名非空）完全相同。
func (a *Activator) Restore(ctx context.Context, r remote.Runner, entries []WriteEntry) ([]RestoreOutcome, error) {
	allowed, err := a.AssertRestorable(entries)
	if err != nil {
		return nil, err
	}
	outcomes := make([]RestoreOutcome, 0, len(allowed))
	if r == nil {
		return nil, fmt.Errorf("缺少远端执行器，无法还原")
	}
	for _, entry := range allowed {
		outcomes = append(outcomes, restoreEntry(ctx, r, entry))
	}
	return outcomes, nil
}

// restoreEntry 在节点上执行一次还原。
//
// 备份存在性判定与删除/复制必须在**同一条命令**里：分两次往返会留下 TOCTOU 窗口
// （探测时备份在、动手前被另一次还原消费掉），那正是"无备份仍删"的另一种形态。
func restoreEntry(ctx context.Context, r remote.Runner, entry WriteEntry) RestoreOutcome {
	outcome := RestoreOutcome{Path: entry.Path, State: "failed"}
	if entry.BackupPath == "" {
		outcome.Detail = "未登记备份，拒绝删除"
		return outcome
	}
	res := remote.RunSplit(ctx, r, buildRestoreCommand(entry))
	// 通道异常与远端非零退出是两件事：Runner 对"脚本以约定码终结"也会带回
	// 一个 exit error（ssh.ExitError / exec.ExitError），此时 ExitCode > 0 是**结论**，
	// 不是故障。把它当通道异常会让 no_backup(21) 被误报成 failed，幂等重放就失效了。
	// 约定与 remote/fs 一致：ExitCode < 0 才表示没有退出码可言。
	if res.ExitCode < 0 {
		outcome.Detail = firstNonEmptyText(errText(res.Err), res.Stderr, "channel_broken")
		return outcome
	}
	switch res.ExitCode {
	case 0:
		outcome.State = "restored"
	case restoreExitNoBackup:
		outcome.State = "skipped"
		outcome.Detail = "no_backup"
	case restoreExitCopyFailed:
		outcome.Detail = firstNonEmptyText(res.Stderr, "copy_failed")
	case restoreExitVerifyFailed:
		outcome.Detail = firstNonEmptyText(res.Stderr, "verify_mismatch")
	case restoreExitMoveFailed:
		outcome.Detail = firstNonEmptyText(res.Stderr, "move_aside_failed")
	default:
		outcome.Detail = firstNonEmptyText(res.Stderr, fmt.Sprintf("exit=%d", res.ExitCode))
	}
	return outcome
}

// buildRestoreCommand 渲染还原脚本。
//
// 文件：cp -p 保留原件权限位（spc 清单是 0644，用写入协议的 chmod 0755 会改权限），
// 随后 cmp 逐字节自证——还原也是写入，不能因为"字节没经过 SSH"就免掉校验。
// 目录树：先把现场挪到 .aside 再 cp -a 备份；复制失败就把现场挪回来。
// rm -rf 只出现在"备份已确认存在"之后，且删的是 .aside 副本或复制失败的半成品，
// 现场唯一副本在任何分支里都还在（要么在 path，要么在 .aside）。
func buildRestoreCommand(entry WriteEntry) string {
	path := remote.ShellQuote(entry.Path)
	backup := remote.ShellQuote(entry.BackupPath)
	if !entry.IsDirTree {
		return "if [ ! -f " + backup + " ]; then exit " + itoa(restoreExitNoBackup) + "; fi; " +
			"cp -p " + backup + " " + path + " || exit " + itoa(restoreExitCopyFailed) + "; " +
			"cmp -s " + backup + " " + path + " || exit " + itoa(restoreExitVerifyFailed)
	}
	aside := remote.ShellQuote(entry.Path + ".aside")
	return "if [ ! -d " + backup + " ]; then exit " + itoa(restoreExitNoBackup) + "; fi; " +
		"if [ -e " + path + " ]; then mv -f " + path + " " + aside + " || exit " + itoa(restoreExitMoveFailed) + "; fi; " +
		"if cp -a " + backup + " " + path + "; then rm -rf " + aside + "; " +
		"else rm -rf " + path + "; if [ -e " + aside + " ]; then mv -f " + aside + " " + path + "; fi; " +
		"exit " + itoa(restoreExitCopyFailed) + "; fi"
}

func itoa(value int) string { return fmt.Sprintf("%d", value) }

func errText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func firstNonEmptyText(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
