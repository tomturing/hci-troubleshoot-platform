package main

// spc 扇出与终态恢复（单片原语见 log_collect_spc.go，本文件只做编排）。
//
// 第一性原理：spc 是"节点上的本地文件"，不是"随命令下发的参数"。
// 一次自定义场景因此等于三段彼此独立的动作，失败语义完全不同，必须分别证明：
//  1. 集合性写文件 —— 写不全就绝不能下发。只写发起节点，其余节点会继续用原清单
//     收集且不报任何错（spc 缺失只是 warning），这是本链路最危险的"看起来成功了"。
//  2. 一次 acli 调用 —— 采集范围必须与写入集合完全相同，所以写完后用显式
//     -i <ip,ip> 下发，而不是继续用 all（all 会让没写到的节点也参与）。
//  3. 集合性还原 —— 任务终态必须把文件换回原件，否则下一次采集甚至厂商后续操作
//     都会踩到平台清单；还原不了要显式报告，不能静默留污染。
//
// 对抗性审查（这些条件会让朴素的"写一遍就行"彻底失败）：
//   - bridge 进程随时被杀：恢复不能只靠内存协程，备份必须自描述（.platform.<task>.bak），
//     下一次写入前先自愈，平台还能显式调 restore_spc 兜底。
//   - 备份层层覆盖：不先还原上一轮的备份，本次备份会把"上一次的平台内容"当原件存下来，
//     原件就永久丢了。
//   - 节点不可达 vs 节点环境坏是两件事：前者本轮不参与（可从采集范围里剔除），
//     后者必须停手（继续采会得到"部分节点自定义、部分节点原清单"的混合结果）。
//   - ssh 必须 BatchMode：没有免密时若允许交互，一条命令就能吃掉整个 180s 动作预算。

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/logpush"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/sshx"
	"time"

	"terminal_bridge/internal/shared/reconcile"
)

const (
	// spcFanoutConcurrency 是并发上限：每台主机要跑 3 次 SSH（探测/备份/写入），
	// 全串行排不进 180s 动作预算，全并发又会打满被采节点的 sshd。
	spcFanoutConcurrency = 4
	// spcHostCommandTimeout 是单台单步上限：BatchMode + ConnectTimeout=5 之后
	// 最坏 5s 建连，30s 能覆盖 find 与 base64 回读校验，又能把"某台卡住"限制在局部。
	spcHostCommandTimeout = 30 * time.Second
	// spcRestorePollInterval 是终态恢复的轮询节奏：acli 进度粒度约 5s，15s 足够快且不添负担。
	spcRestorePollInterval = 15 * time.Second
	// spcRestoreMaxWait 是恢复的兜底期限：即使一直读不到终态，到点也要恢复。
	spcRestoreMaxWait = 2 * time.Hour
	// spcRestoreIdlePolls 是"从未 running"的空轮询上限，与 create 接单确认的 90s 宽限对齐：
	// 超过即认定任务没建立，恢复原件而不是无限等下去。
	spcRestoreIdlePolls = 8
	// spcRestoreFailPolls 是进度连续失败上限：说明会话已失效，交给平台 restore_spc 兜底。
	spcRestoreFailPolls = 3
	// spcRestoreActionBudget 是一次恢复动作的整体预算（含逐台并发）。
	spcRestoreActionBudget = 180 * time.Second
	// spcTrackedTaskLimit 限制注册表容量：bridge 是常驻进程，不能无界增长。
	spcTrackedTaskLimit = 32
)

// 逐台失败根因码（内部码，不是 LC-* 业务码）。
// 固定路径方案下不再产生 spc_dir_ambiguous：目录唯一，不存在"择一"场景。
const (
	spcErrUnreachable = "host_unreachable"
	spcErrDirMissing  = "spc_dir_missing"
	spcErrFileMissing = "spc_file_missing"
	spcErrBackup      = "backup_failed"
	spcErrWrite       = "write_failed"
	// spcErrBusy 是主机键互斥的结论（B3）：另一条链路正在同一台上写/还原。
	// 不是环境故障，稍后重试即可，映射到可重试的 LC-SPC-011。
	spcErrBusy = "host_busy"
)

// spcHostGuard 按裸主机键串行化"场景写入"（B3）：两个任务同时写同一台时，
// 后写者会静默覆盖先写者的清单，采集用哪份完全看时序，产物无法解释。
// 与 reconcile.Run 的 kind@host 键不同命名空间，不会自己锁死自己。
var spcHostGuard = reconcile.NewGuard("")

// spcWriteFanout 是写入/回滚的编排器：并发 4、主机键互斥。
// HostTimeout 取负禁用主机级超时：逐台动作含探测+逐槽位备份/写入多条命令，
// 每条的 30s 预算已在 runSpcOnHostStreams 内部强制，再套 30s 主机级
// 会把多槽位写入误杀成"单台超时"。
func spcWriteFanout() reconcile.Fanout {
	return reconcile.Fanout{
		Concurrency: spcFanoutConcurrency,
		HostTimeout: -1,
		BatchMode:   true,
		Guard:       spcHostGuard,
	}
}

// spcRestoreFanout 是还原的编排器：**不占主机锁**。
// 还原是幂等 cp -p（从不删除），占锁反而制造"终态还原被新任务挡住→
// 意图滞留"的新失败模式；写入侧互斥 + 写前自愈已保证交错最终收敛到原件。
func spcRestoreFanout() reconcile.Fanout {
	return reconcile.Fanout{
		Concurrency: spcFanoutConcurrency,
		HostTimeout: -1,
		BatchMode:   true,
	}
}

// spcHostState 是一台目标主机的写入 / 恢复结果。
// spcHostSlotState 是一台主机上一个槽位的写入/还原证据。
// 逐槽位保存而不是只记"这台写成功了"：多槽位下某个模块文件缺失或校验失败时，
// 必须能回答"到底哪个槽位挡住了下发"，否则排障只能靠猜。
type spcHostSlotState struct {
	Slot         string `json:"slot"`
	Path         string `json:"path,omitempty"`
	Backup       string `json:"backup,omitempty"`
	OriginalSHA  string `json:"original_sha256,omitempty"`
	Written      bool   `json:"written"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorDetail  string `json:"error_detail,omitempty"`
	RestoreState string `json:"restore_state,omitempty"`
	RestoreError string `json:"restore_error,omitempty"`
}

type spcHostState struct {
	IP          string `json:"ip"`
	Dir         string `json:"dir,omitempty"`
	Path        string `json:"path,omitempty"`
	Backup      string `json:"backup,omitempty"`
	OriginalSHA string `json:"original_sha256,omitempty"`
	Written     bool   `json:"written"`
	Prepared    bool   `json:"prepared"`
	ErrorCode   string `json:"error_code,omitempty"`
	ErrorDetail string `json:"error_detail,omitempty"`
	// Slots 为多槽位证据；单槽位通道保持为空，兼容既有回执与测试。
	Slots []spcHostSlotState `json:"slots,omitempty"`
	// RestoreState 取值：pending / restored / skipped / failed。
	RestoreState string `json:"restore_state,omitempty"`
	RestoreError string `json:"restore_error,omitempty"`
}

// spcTaskSnapshot 是给 progress 回执用的轻量视图。
//
// 为什么不能带全量 hosts：progress 每 5s 一次并逐次落库，每台主机带上路径与备份名
// 会让单次回包膨胀到几 KB，写放大远大于它带来的价值；失败项只留 IP + 根因码。
type spcTaskSnapshot struct {
	TaskID       string           `json:"task_id"`
	Slot         string           `json:"slot"`
	Slots        []string         `json:"slots,omitempty"`
	HostCount    int              `json:"host_count"`
	Written      int              `json:"written"`
	Failed       int              `json:"failed"`
	RestoreState string           `json:"restore_state"`
	RestoreError string           `json:"restore_error,omitempty"`
	Failures     []spcHostFailure `json:"failures,omitempty"`
	UpdatedAt    string           `json:"updated_at,omitempty"`
}

// spcHostFailure 是失败主机的精简描述。
type spcHostFailure struct {
	IP   string `json:"ip"`
	Code string `json:"code"`
}

// spcTaskState 保存一次自定义场景的现场，供终态恢复与回执使用。
//
// 内存态不是唯一真相：备份文件名已编码 task，进程重启后 restore_spc 仍可按
// 「重新探测目录 + 拼出的备份名」把文件找回来，所以这里丢状态不会造成数据风险。
type spcTaskState struct {
	TaskID    string
	Slot      string
	Slots     []string
	Targets   []string
	LocalIPs  map[string]bool
	CreatedAt time.Time

	mu           sync.Mutex
	hosts        []spcHostState
	restoreState string
	restoreError string
	everRunning  bool
	restoreTaken bool
	updatedAt    time.Time

	// activator 登记本任务真正写成功的槽位（B1 判定部分）：
	// 回滚前提 = 已登记写入集 ∧ 备份名非空，设备脚本再保证备份确实存在。
	activator *reconcile.Activator
	// written 是骨架维护的写入集：回滚只碰它，不再从结果里手工挑一遍。
	written reconcile.WrittenSet
}

func (st *spcTaskState) setHosts(hosts []spcHostState) {
	st.mu.Lock()
	defer st.mu.Unlock()
	st.hosts = hosts
	st.updatedAt = time.Now()
}

func (st *spcTaskState) hostsCopy() []spcHostState {
	st.mu.Lock()
	defer st.mu.Unlock()
	out := make([]spcHostState, len(st.hosts))
	copy(out, st.hosts)
	return out
}

// dirs 返回已知的 IP → spc 目录映射，恢复阶段优先复用，省掉一次 find。
func (st *spcTaskState) dirs() map[string]string {
	out := map[string]string{}
	for _, host := range st.hostsCopy() {
		if host.Dir != "" {
			out[host.IP] = host.Dir
		}
	}
	return out
}

// noteState 记录观察到的节点态，running 一旦出现就不能再撤回：
// 终态恢复必须区分"跑过之后消失"与"从来没跑起来"。
func (st *spcTaskState) noteState(state string) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if state == logCollectNodeRunning {
		st.everRunning = true
	}
	st.updatedAt = time.Now()
}

func (st *spcTaskState) hasRun() bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.everRunning
}

// beginRestore 保证同一任务只触发一次恢复；返回 false 表示已被触发过。
func (st *spcTaskState) beginRestore() bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	if st.restoreTaken {
		return false
	}
	st.restoreTaken = true
	st.restoreState = "running"
	st.updatedAt = time.Now()
	return true
}

func (st *spcTaskState) finishRestore(state, reason string, hosts []spcHostState) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if state != "" {
		st.restoreState = state
	}
	st.restoreError = reason
	if hosts != nil {
		st.hosts = hosts
	}
	st.updatedAt = time.Now()
}

func (st *spcTaskState) snapshot() spcTaskSnapshot {
	st.mu.Lock()
	defer st.mu.Unlock()
	snapshot := spcTaskSnapshot{
		TaskID:       st.TaskID,
		Slot:         st.Slot,
		Slots:        append([]string(nil), st.Slots...),
		HostCount:    len(st.hosts),
		RestoreState: firstNonEmpty(st.restoreState, "pending"),
		RestoreError: st.restoreError,
	}
	if !st.updatedAt.IsZero() {
		snapshot.UpdatedAt = st.updatedAt.UTC().Format(time.RFC3339)
	}
	for _, host := range st.hosts {
		if host.Written {
			snapshot.Written++
		} else {
			snapshot.Failed++
		}
		if !host.Written && len(snapshot.Failures) < 8 {
			snapshot.Failures = append(snapshot.Failures, spcHostFailure{IP: host.IP, Code: host.ErrorCode})
		}
	}
	return snapshot
}

// spcTaskRegistry 保存最近若干个任务的现场。
type spcTaskRegistry struct {
	mu    sync.Mutex
	tasks map[string]*spcTaskState
}

var spcTaskStore = &spcTaskRegistry{tasks: map[string]*spcTaskState{}}

func (r *spcTaskRegistry) put(st *spcTaskState) {
	if st == nil || st.TaskID == "" {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.tasks == nil {
		r.tasks = map[string]*spcTaskState{}
	}
	r.tasks[st.TaskID] = st
	if len(r.tasks) <= spcTrackedTaskLimit {
		return
	}
	// 超限时淘汰最早创建的任务：备份自描述、恢复可重放，淘汰现场不会造成数据风险，
	// 但要保证当前任务不会被自己挤掉。
	oldestID := ""
	var oldest time.Time
	for id, task := range r.tasks {
		if id == st.TaskID {
			continue
		}
		if oldestID == "" || task.CreatedAt.Before(oldest) {
			oldestID, oldest = id, task.CreatedAt
		}
	}
	if oldestID != "" {
		delete(r.tasks, oldestID)
	}
}

func (r *spcTaskRegistry) get(taskID string) *spcTaskState {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.tasks[strings.TrimSpace(taskID)]
}

func (r *spcTaskRegistry) reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.tasks = map[string]*spcTaskState{}
}

// spcRejection 是可以直接回给前端的拒绝结论。
// Code 是规范 LC-* 码（LC-SPC-001..010），ErrType 保留内部短名供聚合。
type spcRejection struct {
	ErrType string
	Code    string
	Message string
}

// spcRestorePlan 描述一次恢复要做的事；不依赖内存状态，平台显式调用时也能用。
type spcRestorePlan struct {
	Slot     string
	Slots    []string
	TaskID   string
	Targets  []string
	LocalIPs map[string]bool
	Dirs     map[string]string
}

// spcSlotFile 是归一化后的单槽位写入输入。
type spcSlotFile struct {
	Slot    string
	Content string
	SHA256  string
}

// spcApplySpec 是写入所需的全部输入。
// Files 非空表示多槽位；为空时退回单槽位字段，保证存量自定义场景零改动。
type spcApplySpec struct {
	Slot    string
	TaskID  string
	Content string
	Files   []spcSlotFile
}

// files 归一化成待写入槽位列表，顺序即下发 -m 的模块顺序。
func (s spcApplySpec) files() []spcSlotFile {
	if len(s.Files) > 0 {
		return s.Files
	}
	return []spcSlotFile{{Slot: s.Slot, Content: s.Content, SHA256: spcSHA256(s.Content)}}
}

// spcTargetSet 决定本次要写哪些节点。
//
// all 语义必须落到"节点清单"上：拿不到清单就拒绝，绝不退化成"只写发起节点"——
// 那正是本功能要解决的问题本身。
func spcTargetSet(ips string, hosts []logCollectHostOption) ([]string, bool, error) {
	value := strings.TrimSpace(ips)
	if value == "" || strings.EqualFold(value, "all") {
		targets := make([]string, 0, len(hosts))
		for _, host := range hosts {
			if ip := strings.TrimSpace(host.IP); ip != "" {
				targets = append(targets, ip)
			}
		}
		if len(targets) == 0 {
			return nil, true, fmt.Errorf("节点清单为空，无法确定要写哪些节点")
		}
		return spcDedupeIPs(targets), true, nil
	}
	list := make([]string, 0, 4)
	for _, part := range strings.Split(value, ",") {
		ip := strings.TrimSpace(part)
		if ip == "" {
			continue
		}
		if !spcValidIPv4(ip) {
			return nil, false, fmt.Errorf("非法主机 IP：%s", ip)
		}
		list = append(list, ip)
	}
	if len(list) == 0 {
		return nil, false, fmt.Errorf("未指定目标主机")
	}
	return spcDedupeIPs(list), false, nil
}

func spcDedupeIPs(list []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(list))
	for _, ip := range list {
		if seen[ip] {
			continue
		}
		seen[ip] = true
		out = append(out, ip)
	}
	sort.Strings(out)
	return out
}

// spcValidIPv4 与 buildLogCollectCreateCommand 使用同一套判定：
// 每个点分段必须是 0-255，避免把 "1.2.3.999" 这类字符串拼进 ssh 目标。
func spcValidIPv4(ip string) bool {
	if !logCollectIPv4Re.MatchString(ip) {
		return false
	}
	for _, part := range strings.Split(ip, ".") {
		if len(part) > 1 && part[0] == '0' {
			return false
		}
		value := 0
		for _, ch := range part {
			if ch < '0' || ch > '9' {
				return false
			}
			value = value*10 + int(ch-'0')
		}
		if value > 255 {
			return false
		}
	}
	return true
}

// parseSpcLocalAddresses 归一化本机地址探测结果。
// 命中本机的主机直接在当前会话执行，绕开"节点能否 ssh 自己"这个额外前提。
func parseSpcLocalAddresses(output string) map[string]bool {
	out := map[string]bool{}
	for _, field := range strings.Fields(strings.ReplaceAll(output, "\r", "\n")) {
		ip := field
		if index := strings.Index(ip, "/"); index > 0 {
			ip = ip[:index]
		}
		if spcValidIPv4(ip) {
			out[ip] = true
		}
	}
	return out
}

// buildSpcLocalAddressCommand 探测本机 IPv4。
//
// 为什么必须两种方式都试：HCI 节点上 hostname 的 -I 参数依赖 coreutils/busybox 版本，
// 部分裁剪系统直接报错；ip 命令则可能没装。任一成功即可，都失败就退化为"全部走 ssh"。
func buildSpcLocalAddressCommand() string {
	return "hostname -I 2>/dev/null; ip -4 -o addr show 2>/dev/null | awk '{print $4}'"
}

// buildSpcDirCommand 返回固定路径存在性检查脚本。
// 2026-11-17：hint 参数保留仅为兼容旧签名，固定路径方案下忽略；
// 不再用 find 探测（多目录 + exit 1 曾导致 LC-SPC-001 误报）。
func buildSpcDirCommand(_ string) string {
	return buildSpcDirProbe()
}

// buildSpcSSHCommand 拼出"从发起节点跳到目标节点"的命令。
//
// 关键约束：
//   - BatchMode=yes：没有免密就立刻失败，绝不能让 ssh 在节点上等密码输入；
//   - ConnectTimeout=5：不可达节点必须在 5s 内失败，否则并发窗口会被一台死机拖满；
//   - root@：bridge 登录用户已在 main.go 归一为 root，写 spc 目录依赖这一点。
func buildSpcSSHCommand(ip, script string) string {
	return fmt.Sprintf("ssh -o BatchMode=yes -o ConnectTimeout=5 root@%s %s", ip, shellQuote(script))
}

// buildSpcPrepareCommand 完成「目录重建 + 写前自愈 + 备份 + 备份保真校验」。
//
// 目录重建必须最先做：acli 重装会清掉整个插件目录（含 spc），目录不在时
// 后面的备份/写入全部失败。该目录是平台自有固定路径，mkdir -p 安全。
//
// 自愈顺序不可颠倒：先把历史备份（原件）放回去，再备份，否则本次备份存下的是
// 上一次的平台清单，原件会被永久覆盖。本次任务的备份已存在时不再覆盖，
// 保证重试写同一个任务不会把原件换成平台内容。
func buildSpcPrepareCommand(target, backup string) string {
	dir := target
	if idx := strings.LastIndex(target, "/"); idx > 0 {
		dir = target[:idx]
	}
	ensureDir := fmt.Sprintf("mkdir -p %s", shellQuote(dir))
	// 确定性 tmp 的崩溃残留清理：argv 写入协议先写 <target>.tmp 再 mv，
	// 断链/进程被杀会留下半截 tmp；不清掉它虽不影响正确性（下次写入会覆盖），
	// 但会永久占着磁盘并在排查时误导"是不是写了一半"。
	cleanTmp := fmt.Sprintf("rm -f %s", shellQuote(target+".tmp"))
	glob := fmt.Sprintf("%s.platform.*.bak", shellQuote(target))
	prev := fmt.Sprintf("__prev=$(ls -1t %s 2>/dev/null | grep -v -F %s | head -n 1)", glob, shellQuote(backup))
	selfHeal := fmt.Sprintf("if [ -n \"$__prev\" ]; then cp -p \"$__prev\" %s 2>/dev/null; fi", shellQuote(target))
	// 校验只在"本次刚创建备份"的同一分支里做：同一任务重试时备份已存在，
	// 此时 target 可能就是上一轮写入的平台清单，再比对必然失败并误判环境异常。
	backupCmd := fmt.Sprintf("if [ ! -f %s ]; then cp -p %s %s 2>/dev/null || { printf 'SPC_PREPARE_FAIL reason=backup_failed\\n'; exit 0; }; "+
		"cmp -s %s %s || { printf 'SPC_PREPARE_FAIL reason=backup_mismatch\\n'; exit 0; }; fi",
		shellQuote(backup), shellQuote(target), shellQuote(backup), shellQuote(target), shellQuote(backup))
	orig := fmt.Sprintf("__orig=$(sha256sum %s 2>/dev/null | awk '{print $1}')", shellQuote(backup))
	return fmt.Sprintf("%s; %s; %s; %s; %s; %s; printf 'SPC_PREPARE_OK orig=%%s\\n' \"$__orig\"", ensureDir, cleanTmp, prev, selfHeal, backupCmd, orig)
}

// buildSpcWriteVerifyCommand 写入并回读校验，任一步失败立刻用备份回滚。
//
// 校验把原文经 printf 再传一遍、用 cmp 与盘上文件逐字节比对：HCI 宿主机是极简
// 系统，base64 可能缺失（2026-11-17 Q2026110302166 现场 base64 缺失致写入 exit 127），
// sha256sum 也可能缺失；cmp 已是 prepare 阶段（备份保真校验）的硬依赖，不新增工具假设。
// 不能用 $(cat) 回读进变量再比较：命令替换会剥掉末尾换行，清单原文若以换行结尾
// （合法 JSON 空白）会被误判 verify_mismatch，写对了也回滚。
func buildSpcWriteVerifyCommand(target, backup, content string) string {
	write := buildSpcWriteCommand(target, content)
	verify := fmt.Sprintf("printf '%%s' %s | cmp -s - %s", shellQuote(content), shellQuote(target))
	rollback := buildSpcRestoreCommand(backup, target)
	// 写入段包在子 shell 里：argv 写入协议以 umask 077 开头，本机直连模式下
	// 脚本跑在交互会话里，不包住会把会话 umask 永久改成 077，
	// 后续其它命令创建的文件全部变成 0600。
	return fmt.Sprintf("( %s ) || { %s 2>/dev/null; printf 'SPC_WRITE_FAIL reason=write_rejected\\n'; exit 0; }; "+
		"%s || { %s 2>/dev/null; printf 'SPC_WRITE_FAIL reason=verify_mismatch\\n'; exit 0; }; "+
		"printf 'SPC_WRITE_OK\\n'", write, rollback, verify, rollback)
}

// buildSpcRestoreScript 恢复原件；备份不存在时报告 skip（幂等重放的关键）。
func buildSpcRestoreScript(target, backup string) string {
	return fmt.Sprintf("if [ ! -f %s ]; then printf 'SPC_RESTORE_SKIP reason=no_backup\\n'; exit 0; fi; "+
		"%s || { printf 'SPC_RESTORE_FAIL reason=copy_failed\\n'; exit 0; }; "+
		"cmp -s %s %s && printf 'SPC_RESTORE_OK\\n' || printf 'SPC_RESTORE_FAIL reason=verify_mismatch\\n'",
		shellQuote(backup), buildSpcRestoreCommand(backup, target), shellQuote(backup), shellQuote(target))
}

// spcMarkerHas 判断输出里是否出现过指定标记行。
func spcMarkerHas(output, marker string) bool {
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r", ""), "\n") {
		line = strings.TrimSpace(line)
		if line == marker || strings.HasPrefix(line, marker+" ") {
			return true
		}
	}
	return false
}

// spcMarkerField 从标记行里取 key=value。
func spcMarkerField(output, key string) string {
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r", ""), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "SPC_") {
			continue
		}
		for _, field := range strings.Fields(line) {
			if parts := strings.SplitN(field, "=", 2); len(parts) == 2 && parts[0] == key {
				return parts[1]
			}
		}
	}
	return ""
}

// spcRestoreDue 判断是否该恢复原件。返回 (是否, 触发原因)。
//
// 只有终态与"跑过之后消失"才算完成；从没 running 过就消失，要等满空轮询窗口，
// 否则一次还没接单的下发会把清单提前换回去（采集随后才开始，等于用原清单采）。
func spcRestoreDue(nodeState string, everRunning bool, idlePolls int) (bool, string) {
	switch nodeState {
	case logCollectNodeCompleted:
		return true, "task_completed"
	case logCollectNodeAbnormal:
		return true, "task_abnormal"
	case logCollectNodeNone:
		if everRunning {
			return true, "task_gone"
		}
		if idlePolls >= spcRestoreIdlePolls {
			return true, "never_started"
		}
	}
	return false, ""
}

// runSpcOnHostStreams 在目标主机上执行一段脚本：本机直连当前会话，其余节点走一次 ssh。
//
// 为什么 stdout 与 stderr 必须分开返回：
//  1. stderr 是「真实原因」的唯一来源（read-only file system、permission denied、
//     cp: cannot stat）。旧实现走 runQuickCommand，它只在 stdout 为空时才拿 stderr
//     兜底——脚本先打赢了 marker 再失败时，原因就被静默丢弃，界面只剩一句
//     「场景清单写入失败」（2026-11-12 Q2026110302155 现场）。
//  2. stderr 不能直接拼进 stdout：目录探测把任何以 "/" 开头的行当候选目录，
//     "/bin/sh: line 1: ..." 这类行会被算成第二个目录，凭空报「目录不唯一」。
func runSpcOnHostStreams(s *sshx.SSHSession, ctx context.Context, ip string, local bool, script string) (stdout, stderr string, exitCode int, err error) {
	command := script
	if !local {
		command = buildSpcSSHCommand(ip, script)
	}
	hostCtx, cancel := context.WithTimeout(ctx, spcHostCommandTimeout)
	defer cancel()
	return s.RunQuickCommandStreams(hostCtx, command)
}

// spcNodeList 取集群节点清单（与 probe 同源命令），用于把 all 解析成具体主机集合。
func spcNodeList(s *sshx.SSHSession, ctx context.Context) []logCollectHostOption {
	probeCtx, cancel := context.WithTimeout(ctx, spcHostCommandTimeout)
	defer cancel()
	output, _, err := runCommandPty(s, probeCtx, logCollectAcliInvoke("--formatter json platform node list"), spcHostCommandTimeout)
	if err != nil {
		return nil
	}
	return parseLogCollectNodeList(output)
}

// spcLocalAddresses 探测本机 IPv4；探测失败返回空集合（等价于全部走 ssh）。
func spcLocalAddresses(s *sshx.SSHSession, ctx context.Context) map[string]bool {
	probeCtx, cancel := context.WithTimeout(ctx, spcHostCommandTimeout)
	defer cancel()
	output, _, err := s.RunQuickCommand(probeCtx, buildSpcLocalAddressCommand())
	if err != nil {
		return map[string]bool{}
	}
	return parseSpcLocalAddresses(output)
}

// parseSpcFiles 校验平台签发的逐槽位清单。
// 槽位必须是 acli 合法模块、内容必须过 spc 二级校验、摘要必须自洽：
// 任何一项不合规都拒绝下发，绝不带着半份清单去改节点文件。
func parseSpcFiles(msg models.InMessage) ([]spcSlotFile, *spcRejection) {
	if len(msg.SpcFiles) == 0 {
		return nil, nil
	}
	if strings.TrimSpace(msg.SpcJSON) != "" {
		return nil, &spcRejection{ErrType: "spc_config_conflict", Message: "spc_files 与 spc_json 不能同时下发"}
	}
	seen := map[string]bool{}
	files := make([]spcSlotFile, 0, len(msg.SpcFiles))
	for _, raw := range msg.SpcFiles {
		slot := strings.TrimSpace(raw.Slot)
		if !spcSlotRe.MatchString(slot) || !logCollectModules[slot] {
			return nil, &spcRejection{ErrType: "invalid_spc", Message: fmt.Sprintf("不支持的清单槽位：%s", slot)}
		}
		if seen[slot] {
			return nil, &spcRejection{ErrType: "invalid_spc", Message: fmt.Sprintf("清单槽位重复：%s", slot)}
		}
		seen[slot] = true
		if len(raw.Content) == 0 || len(raw.Content) > logCollectSpcMaxBytes {
			return nil, &spcRejection{ErrType: "invalid_spc", Message: fmt.Sprintf("槽位 %s 的清单为空或超过 %d 字节", slot, logCollectSpcMaxBytes)}
		}
		if err := validateSpcContent(raw.Content); err != nil {
			return nil, &spcRejection{ErrType: "invalid_spc", Message: fmt.Sprintf("槽位 %s 的清单非法：%v", slot, err)}
		}
		digest := spcSHA256(raw.Content)
		if declared := strings.ToLower(strings.TrimSpace(raw.SHA256)); declared != "" && declared != digest {
			return nil, &spcRejection{ErrType: "spc_digest_mismatch", Message: fmt.Sprintf("槽位 %s 的清单摘要与内容不一致（传输可能被截断或改写）", slot)}
		}
		files = append(files, spcSlotFile{Slot: slot, Content: raw.Content, SHA256: digest})
	}
	return files, nil
}

// prepareSpcScenario 完成「解析目标主机 + 逐台逐槽位写入 + 全量校验」，成功才返回现场。
//
// 任何一台、任一槽位写不进去都回滚并拒绝：宁可这一次不发任务，也不能让节点之间
// 用不同的清单收集——那种结果事后无法从采集结果里看出来。
func prepareSpcScenario(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, msg models.InMessage, traceID, customUI string) (*spcTaskState, *spcRejection) {
	files, rejection := parseSpcFiles(msg)
	if rejection != nil {
		return nil, rejection
	}
	multi := len(files) > 0

	slot := strings.TrimSpace(msg.SpcSlot)
	if slot == "" {
		slot = logCollectSpcSlot
	}
	if multi {
		// 多槽位时 SpcSlot 只是兼容字段，不能覆盖平台签发的槽位集合。
		slot = files[0].Slot
	} else if slot != logCollectSpcSlot {
		return nil, &spcRejection{ErrType: "spc_slot_unsupported", Message: fmt.Sprintf("不支持的 spc 槽位：%s（bridge 仅支持 %s）", slot, logCollectSpcSlot)}
	}

	spec := spcApplySpec{Slot: slot}
	var digest string
	var spcBytes int
	var slots, digests []string
	if multi {
		spec.Files = files
		for _, file := range files {
			slots = append(slots, file.Slot)
			digests = append(digests, file.Slot+":"+file.SHA256)
			spcBytes += len(file.Content)
		}
		digest = spcSHA256(strings.Join(digests, "|"))
	} else {
		if err := validateSpcContent(msg.SpcJSON); err != nil {
			return nil, &spcRejection{ErrType: "invalid_spc", Message: fmt.Sprintf("场景内容非法：%v", err)}
		}
		digest = spcSHA256(msg.SpcJSON)
		if declared := strings.ToLower(strings.TrimSpace(msg.SpcSHA256)); declared != "" && declared != digest {
			return nil, &spcRejection{ErrType: "spc_digest_mismatch", Message: "场景摘要与内容不一致（传输可能被截断或改写）"}
		}
		spec.Content = msg.SpcJSON
		spcBytes = len(msg.SpcJSON)
	}

	taskID := strings.TrimSpace(msg.ExecID)
	if !spcTaskIDRe.MatchString(taskID) {
		return nil, &spcRejection{ErrType: "invalid_task_id", Message: "缺少合法的任务 ID，无法确定备份归属"}
	}
	spec.TaskID = taskID

	var hosts []logCollectHostOption
	if value := strings.TrimSpace(msg.IPs); value == "" || strings.EqualFold(value, "all") {
		hosts = spcNodeList(s, ctx)
	}
	targets, all, err := spcTargetSet(msg.IPs, hosts)
	if err != nil {
		return nil, &spcRejection{ErrType: "spc_hosts_unknown", Message: err.Error()}
	}

	state := &spcTaskState{
		TaskID:       taskID,
		Slot:         slot,
		Slots:        slots,
		Targets:      targets,
		LocalIPs:     spcLocalAddresses(s, ctx),
		CreatedAt:    time.Now(),
		restoreState: "pending",
		activator:    reconcile.NewActivator(),
	}
	logpush.Blog("INFO", "log_collect.spc_fanout_start", "开始把场景写入目标节点", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
		"task_id": taskID, "slot": slot, "slots": slots, "spc_sha256": digest,
		"spc_digests": digests, "hosts_count": len(targets), "ips_all": all, "spc_bytes": spcBytes,
	})

	results, writtenSet := applySpcAll(b, ctx, s, state, spec)
	state.setHosts(results)
	state.written = writtenSet

	written, failed := spcClassifyHosts(results)
	// all 模式下不可达视为本轮不参与（保持既有语义）；
	// 点名模式下任何一台失败都必须拒绝。
	blocking := failed
	if all {
		blocking = make([]spcHostState, 0, len(failed))
		for _, host := range failed {
			if host.ErrorCode != spcErrUnreachable {
				blocking = append(blocking, host)
			}
		}
	}
	if len(blocking) > 0 || written == 0 {
		// all 模式下不可达不阻断，但一台上都没写成时必须把它报出来：
		// 否则 Message 只剩「目标 N 台，成功 0 台」，用户拿不到任何线索。
		report := blocking
		if len(report) == 0 {
			report = failed
		}
		errType, code := spcRejectionCode(report)
		rejection := &spcRejection{
			ErrType: errType,
			Code:    code,
			Message: spcRejectionMessage(written, report),
		}
		// 只回滚已写入的主机：从未写入的节点不需要恢复，把它计入 restore_failed
		// 会推导出"可能残留平台清单"的假告警（2026-11-17 Q2026110302166 现场：
		// 192.0.2.11 SSH 认证失败、written=false，却被报成回滚失败）。
		restored, restoreFailed := rollbackSpcAll(b, s, state, "写入未通过，已回滚", spcWrittenTargets(results))
		if restoreFailed > 0 {
			// 主因码保持不变：回滚失败是叠加风险，覆盖主因会让用户按错误的码排查。
			rejection.Message = sshx.TruncateString(rejection.Message+
				contractFormat("spc_restore_risk", map[string]string{"count": fmt.Sprint(restoreFailed)}), 560)
		}
		logpush.Blog("ERROR", "log_collect.spc_fanout_failed", "场景写入未通过，已回滚并拒绝下发", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
			"task_id": taskID, "slot": slot, "written": written,
			"error_code": code, "error_type": errType,
			"failed_hosts":   spcHostAuditTrail(blocking),
			"restored_hosts": restored,
			"restore_failed": restoreFailed,
			"reason":         rejection.Message,
		})
		return nil, rejection
	}

	spcTaskStore.put(state)
	// 抗崩溃（§3.2）：终态还原的责任先落盘再交给内存 watcher。
	// bridge 被杀后 watcher 消失，重启时凭意图 + 自描述备份名收尾。
	persistSpcRestoreIntent(b, state, traceID, msg.CaseID, msg.NodeIP, customUI)
	logpush.Blog("INFO", "log_collect.spc_fanout", "场景已写入全部目标节点", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
		"task_id": taskID, "slot": slot, "spc_sha256": digest,
		"written": written, "failed_hosts": spcHostAuditTrail(failed),
		"hosts": spcHostAuditTrail(results),
	})
	return state, nil
}

// spcClassifyHosts 返回写入成功台数与失败明细（按 IP 排序）。
// 关键变化：不再把「环境坏」和「不可达」压成两串字符串——
// 字符串会丢掉每台的具体根因码，Message 里就只剩"节点不可达"这类泛化描述。
func spcClassifyHosts(results []spcHostState) (int, []spcHostState) {
	written := 0
	failed := make([]spcHostState, 0, len(results))
	for _, host := range results {
		if host.Written {
			written++
			continue
		}
		failed = append(failed, host)
	}
	sort.Slice(failed, func(i, j int) bool { return failed[i].IP < failed[j].IP })
	return written, failed
}

// spcRejectionCode 选规范码：全部节点同一原因时给精确码；
// 原因不一致时给聚合码 LC-SPC-010，逐台原因仍在 Message 与 detail.spc_hosts 里。
// 不能用某一台的码代表全部：用户会按这个码去排查，而实际有别的故障混在里面。
func spcRejectionCode(failed []spcHostState) (string, string) {
	first := ""
	same := true
	for _, host := range failed {
		code := firstNonEmpty(host.ErrorCode, spcErrWrite)
		if first == "" {
			first = code
			continue
		}
		if code != first {
			same = false
		}
	}
	if first == "" || !same {
		return "spc_write_failed", ErrSpcAggregate
	}
	return first, firstNonEmpty(validationErrorCode(first), ErrSpcAggregate)
}

// spcRejectionMessage 逐台给出「IP — 具体原因」，不再把不同根因揉成"节点不可达"。
// 用户看到这句话就知道该查哪台、查什么；完整 stderr 在 detail.spc_hosts[].error_detail。
func spcRejectionMessage(written int, failed []spcHostState) string {
	total := written + len(failed)
	if total == 0 {
		return contractText["spc_rejection_empty"]
	}
	head := contractFormat("spc_rejection_head", map[string]string{
		"total":   fmt.Sprint(total),
		"written": fmt.Sprint(written),
	})
	if len(failed) == 0 {
		return head
	}
	details := make([]string, 0, len(failed))
	for _, host := range failed {
		details = append(details, contractFormat("spc_host_detail", map[string]string{
			"ip":    host.IP,
			"title": spcErrorTitle(firstNonEmpty(host.ErrorCode, spcErrWrite)),
		}))
	}
	joined := contractFormat("spc_detail_join", map[string]string{"details": strings.Join(details, "；")})
	return sshx.TruncateString(head+joined, 500)
}

// spcHostAuditTrail 把逐台结果压成可入库的短结构：路径与备份名是排查恢复问题的唯一线索。
func spcHostAuditTrail(results []spcHostState) []map[string]any {
	trail := make([]map[string]any, 0, len(results))
	for _, host := range results {
		item := spcHostTrailItem(host, "error_code")
		trail = append(trail, item)
	}
	return trail
}

// spcPersistHosts 是回给平台的逐台结果：字段名与前端契约（ip/backup/error）对齐，
// 恢复态随任务一起落库，否则"哪些节点还残留平台清单"只能靠桥日志回溯。
func spcPersistHosts(results []spcHostState) []map[string]any {
	trail := make([]map[string]any, 0, len(results))
	for _, host := range results {
		trail = append(trail, spcHostTrailItem(host, "error"))
	}
	return trail
}

func spcHostTrailItem(host spcHostState, errorKey string) map[string]any {
	item := map[string]any{"ip": host.IP, "written": host.Written}
	if host.Dir != "" {
		item["dir"] = host.Dir
	}
	if host.Backup != "" {
		item["backup"] = host.Backup
	}
	if host.OriginalSHA != "" {
		item["original_sha256"] = host.OriginalSHA
	}
	if host.ErrorCode != "" {
		item[errorKey] = host.ErrorCode
	}
	// stderr 摘要必须落库：只有 error_code 时，同一码下的免密/路径/权限差异无法区分。
	if host.ErrorDetail != "" {
		item["error_detail"] = sshx.TruncateString(host.ErrorDetail, 500)
	}
	// 多槽位逐条证据：检索时要能回答"哪个模块文件没写成/没还原"。
	if len(host.Slots) > 0 {
		item["slots"] = host.Slots
	}
	if host.RestoreState != "" {
		item["restore_state"] = host.RestoreState
	}
	if host.RestoreError != "" {
		item["restore_error"] = host.RestoreError
	}
	return item
}

// writtenTargets 返回真正写入成功的主机（有序），用于把 -i 收窄到与写入集合一致。
// 用 all 下发会让"本轮不可达而未写入"的节点也参与采集，等于用原清单混采。
func (st *spcTaskState) writtenTargets() []string {
	hosts := st.hostsCopy()
	out := make([]string, 0, len(hosts))
	for _, host := range hosts {
		if host.Written && host.IP != "" {
			out = append(out, host.IP)
		}
	}
	sort.Strings(out)
	return out
}

// applySpcAll 并发把场景写到全部目标节点（S3：编排委托 reconcile.Fanout）。
// 并发上限、主机键互斥、written-set 跟踪由骨架维护；逐台动作内部的
// 探测/备份/写入/回读校验仍是产品语义，留在 applySpcToHost。
func applySpcAll(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, state *spcTaskState, spec spcApplySpec) ([]spcHostState, reconcile.WrittenSet) {
	results, written := spcWriteFanout().WriteAll(ctx, state.Targets, func(ctx context.Context, ip string) reconcile.HostResult {
		host := applySpcToHost(b, ctx, s, spec, ip, state.LocalIPs[ip], state.activator)
		return reconcile.HostResult{
			Host: host.IP, Written: host.Written,
			ErrorCode: host.ErrorCode, ErrorDetail: host.ErrorDetail, Value: host,
		}
	})
	hosts := make([]spcHostState, 0, len(results))
	for _, result := range results {
		hosts = append(hosts, spcHostFromResult(result))
	}
	return hosts, written
}

// spcHostFromResult 把骨架结论还原成逐台证据。
// busy/超时这类结论来自动作执行之前，没有逐槽位明细可取；
// 动作已返回但父 ctx 恰好取消时，骨架会叠加错误码——此时必须把
// Written 压回 false，否则回执说写成了而写入集里没有它，两边对不上。
func spcHostFromResult(result reconcile.HostResult) spcHostState {
	if host, ok := result.Value.(spcHostState); ok {
		if result.ErrorCode != "" && host.ErrorCode == "" {
			host.ErrorCode, host.ErrorDetail = result.ErrorCode, result.ErrorDetail
			host.Written = false
		}
		return host
	}
	host := spcHostState{IP: result.Host, ErrorCode: result.ErrorCode, ErrorDetail: result.ErrorDetail}
	if result.ErrorCode == reconcile.CodeBusy {
		host.ErrorCode = spcErrBusy
		host.ErrorDetail = "该节点已有另一次场景写入在进行，稍后重试"
	}
	return host
}

// applySpcToHost 在单台主机上完成目录探测 → 逐槽位备份（含自愈）→ 写入 → 回读校验。
//
// 多槽位下任一槽位失败即把整台标为失败：写不全就绝不能下发，
// 否则部分模块用平台清单、部分用节点原清单，产物无法解释。
func applySpcToHost(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, spec spcApplySpec, ip string, local bool, activator *reconcile.Activator) spcHostState {
	state := spcHostState{IP: ip, RestoreState: "pending"}

	output, errOut, _, err := runSpcOnHostStreams(s, ctx, ip, local, buildSpcDirCommand(""))
	if err != nil {
		state.ErrorCode, state.ErrorDetail = classifySpcCommandError(err, spcJoinDiag(output, errOut), spcStepDir)
		return state
	}
	dir, dirCode := spcResolveDir(output)
	if dirCode != "" {
		state.ErrorCode = dirCode
		return state
	}
	state.Dir = dir

	files := spec.files()
	for _, file := range files {
		slotState := applySpcSlotToHost(b, ctx, s, ip, local, dir, spec.TaskID, file, activator)
		if len(files) > 1 {
			state.Slots = append(state.Slots, slotState)
		}
		// 单槽位兼容：既有回执字段仍指向唯一/首个槽位，存量前端与测试不改也能读。
		if len(state.Slots) <= 1 {
			state.Path, state.Backup, state.OriginalSHA = slotState.Path, slotState.Backup, slotState.OriginalSHA
		}
		if slotState.OriginalSHA != "" {
			state.Prepared = true
		}
		if slotState.ErrorCode != "" {
			state.ErrorCode = slotState.ErrorCode
			state.ErrorDetail = fmt.Sprintf("槽位 %s：%s", file.Slot, firstNonEmpty(slotState.ErrorDetail, slotState.ErrorCode))
			return state
		}
	}
	state.Written = true
	return state
}

// applySpcSlotToHost 在已探测到的目录里完成一个槽位的完整写入闭环。
func applySpcSlotToHost(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, ip string, local bool, dir, taskID string, file spcSlotFile, activator *reconcile.Activator) spcHostSlotState {
	state := spcHostSlotState{Slot: file.Slot}
	target, pathErr := spcFilePath(dir, file.Slot)
	if pathErr != nil {
		state.ErrorCode = spcErrFileMissing
		state.ErrorDetail = pathErr.Error()
		return state
	}
	backup, backupErr := spcBackupPath(target, taskID)
	if backupErr != nil {
		state.ErrorCode = spcErrBackup
		state.ErrorDetail = backupErr.Error()
		return state
	}
	state.Path, state.Backup = target, backup

	prepareOut, prepareErrOut, _, prepareErr := runSpcOnHostStreams(s, ctx, ip, local, buildSpcPrepareCommand(target, backup))
	if prepareErr != nil {
		state.ErrorCode, state.ErrorDetail = classifySpcCommandError(prepareErr, spcJoinDiag(prepareOut, prepareErrOut), spcStepBackup)
		return state
	}
	if !spcMarkerHas(prepareOut, "SPC_PREPARE_OK") {
		state.ErrorCode = firstNonEmpty(spcMarkerField(prepareOut, "reason"), spcErrBackup)
		// 合并 stderr：宿主机缺命令、目录不可写等真实原因只在 stderr 里，
		// 只取 stdout 会只剩一个 backup_failed，现场无法自证。
		state.ErrorDetail = spcOutputTail(spcJoinDiag(prepareOut, prepareErrOut), 2, 200)
		return state
	}
	state.OriginalSHA = spcMarkerField(prepareOut, "orig")

	writeOut, writeErrOut, _, writeErr := runSpcOnHostStreams(s, ctx, ip, local, buildSpcWriteVerifyCommand(target, backup, file.Content))
	if writeErr != nil {
		state.ErrorCode, state.ErrorDetail = classifySpcCommandError(writeErr, spcJoinDiag(writeOut, writeErrOut), spcStepWrite)
		return state
	}
	if !spcMarkerHas(writeOut, "SPC_WRITE_OK") {
		state.ErrorCode = firstNonEmpty(spcMarkerField(writeOut, "reason"), spcErrWrite)
		// 合并 stderr：base64/printf 缺失、重定向被拒等真实原因只在 stderr 里，
		// 之前只取 stdout 导致 error_detail 只剩 write_rejected（Q2026110302166）。
		state.ErrorDetail = spcOutputTail(spcJoinDiag(writeOut, writeErrOut), 2, 200)
		return state
	}
	state.Written = true
	// B1 登记：只有真正写成功的槽位才进入可回滚集合。
	// 备份名按任务隔离（spcBackupPath 已校验），回滚时以登记值为准。
	activator.Register(reconcile.WriteEntry{Path: target, BackupPath: backup})
	return state
}

// spcWrittenTargets 只挑出写入成功的主机 IP。
// 回滚的语义是撤销已完成的写入，从未写入的节点不应参与，否则会把"SSH 不可达"
// 误报成"回滚失败、可能残留平台清单"（Q2026110302166）。
func spcWrittenTargets(results []spcHostState) []string {
	out := make([]string, 0, len(results))
	for _, host := range results {
		if host.Written {
			out = append(out, host.IP)
		}
	}
	return out
}

// rollbackSpcAll 对已写入主机执行回滚，返回 (恢复成功台数, 恢复失败台数)。
// 回滚范围以骨架维护的 written-set 为准：从未写入的节点没有需要撤销的改动，
// 把它们纳入回滚只会制造"可能残留平台清单"的假告警（Q2026110302166）。
func rollbackSpcAll(b *app.Bridge, s *sshx.SSHSession, st *spcTaskState, reason string, targets []string) (int, int) {
	// B1 门：回滚前提 = 已登记写入集 ∧ 备份名非空（判定部分）；
	// "备份确实存在"由设备脚本在同一条命令里保证（if [ ! -f ] → skip）。
	// 门不过就一条命令都不发：fail-closed 报还原失败，绝不凭猜测碰现场。
	if _, err := st.activator.AssertRestorable(st.rollbackEntries()); err != nil {
		results := make([]spcHostState, 0, len(targets))
		for _, ip := range targets {
			results = append(results, spcHostState{
				IP: ip, RestoreState: "failed", RestoreError: spcErrWrite, ErrorDetail: err.Error(),
			})
		}
		st.finishRestore("failed", reason, results)
		return 0, len(targets)
	}
	ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
	defer cancel()
	plan := spcRestorePlan{
		Slot: st.Slot, Slots: st.Slots, TaskID: st.TaskID,
		LocalIPs: st.LocalIPs, Dirs: st.dirs(),
	}
	results := spcRestoreFanout().RollbackWritten(ctx, st.written, func(ctx context.Context, ip string) reconcile.HostResult {
		return spcRestoreResult(restoreSpcOnHost(b, ctx, s, plan, ip))
	})
	hosts := make([]spcHostState, 0, len(results))
	for _, result := range results {
		hosts = append(hosts, spcRestoreHostFromResult(result))
	}
	restored, failed := spcCountRestore(hosts)
	status := "success"
	if failed > 0 {
		status = "failed"
	}
	st.finishRestore(status, reason, hosts)
	return restored, failed
}

// rollbackEntries 拼出本任务全部槽位的"路径+备份名"供 B1 门判定。
// 目录用固定 spcDeployDir：pickSpcDir 只认它，登记时的路径必然同源。
// 未写入的槽位会被 AssertRestorable 剔除，而不是拒绝整个回滚：
// 多槽位下第二个槽位全员写失败时，第一个槽位的已写内容仍须被还原。
func (st *spcTaskState) rollbackEntries() []reconcile.WriteEntry {
	slots := st.Slots
	if len(slots) == 0 {
		slots = []string{st.Slot}
	}
	entries := make([]reconcile.WriteEntry, 0, len(slots))
	for _, slot := range slots {
		target, err := spcFilePath(spcDeployDir, slot)
		if err != nil {
			continue
		}
		backup, err := spcBackupPath(target, st.TaskID)
		if err != nil {
			continue
		}
		entries = append(entries, reconcile.WriteEntry{Path: target, BackupPath: backup})
	}
	return entries
}

// spcRestoreResult 把逐台还原结论交给骨架（只用于计数，不跟踪写入集）。
func spcRestoreResult(host spcHostState) reconcile.HostResult {
	result := reconcile.HostResult{Host: host.IP, Value: host}
	if host.RestoreState == "failed" {
		result.ErrorCode = firstNonEmpty(host.RestoreError, spcErrWrite)
		result.ErrorDetail = host.ErrorDetail
	}
	return result
}

func spcRestoreHostFromResult(result reconcile.HostResult) spcHostState {
	if host, ok := result.Value.(spcHostState); ok {
		return host
	}
	return spcHostState{
		IP: result.Host, RestoreState: "failed",
		RestoreError: firstNonEmpty(result.ErrorCode, spcErrWrite), ErrorDetail: result.ErrorDetail,
	}
}

// restoreSpcHosts 并发恢复全部目标节点（S3：编排委托 reconcile.Fanout.Each）。
func restoreSpcHosts(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, plan spcRestorePlan) []spcHostState {
	results := spcRestoreFanout().Each(ctx, plan.Targets, func(ctx context.Context, ip string) reconcile.HostResult {
		return spcRestoreResult(restoreSpcOnHost(b, ctx, s, plan, ip))
	})
	hosts := make([]spcHostState, 0, len(results))
	for _, result := range results {
		hosts = append(hosts, spcRestoreHostFromResult(result))
	}
	return hosts
}

func restoreSpcOnHost(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, plan spcRestorePlan, ip string) spcHostState {
	state := spcHostState{IP: ip}
	local := plan.LocalIPs[ip]
	dir := plan.Dirs[ip]
	if dir == "" {
		// 固定路径方案：目录由 acli 插件自部署，直接用常量，不再重跑 find 探测。
		// 旧实现在此重探测，双目录 + exit 1 会失败并追加"回滚失败 N 台"假告警（2026-11-17 现场）。
		dir = spcDeployDir
	}
	state.Dir = dir

	slots := plan.Slots
	if len(slots) == 0 {
		slots = []string{plan.Slot}
	}
	restored, skipped, failed := 0, 0, 0
	firstErr := ""
	for _, slot := range slots {
		slotState := restoreSpcSlotOnHost(b, ctx, s, ip, local, dir, plan.TaskID, slot)
		if len(slots) > 1 {
			state.Slots = append(state.Slots, slotState)
		}
		if len(state.Slots) <= 1 {
			state.Path, state.Backup = slotState.Path, slotState.Backup
		}
		switch slotState.RestoreState {
		case "restored":
			restored++
		case "skipped":
			skipped++
		default:
			failed++
			if firstErr == "" {
				firstErr = firstNonEmpty(slotState.RestoreError, spcErrWrite)
				state.ErrorDetail = slotState.ErrorDetail
			}
		}
	}
	// 任一槽位没还原成功就不能报 success：残留一份平台清单就会污染下一次采集。
	switch {
	case failed > 0:
		state.RestoreState = "failed"
		state.RestoreError = firstErr
	case restored > 0:
		state.RestoreState = "restored"
	default:
		state.RestoreState = "skipped"
	}
	return state
}

// restoreSpcSlotOnHost 恢复单个槽位：备份不存在返回 skipped，幂等重放不误报。
func restoreSpcSlotOnHost(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, ip string, local bool, dir, taskID, slot string) spcHostSlotState {
	state := spcHostSlotState{Slot: slot}
	target, pathErr := spcFilePath(dir, slot)
	if pathErr != nil {
		state.RestoreState = "failed"
		state.RestoreError = spcErrFileMissing
		return state
	}
	backup, backupErr := spcBackupPath(target, taskID)
	if backupErr != nil {
		state.RestoreState = "failed"
		state.RestoreError = spcErrBackup
		return state
	}
	state.Path, state.Backup = target, backup

	output, errOut, _, err := runSpcOnHostStreams(s, ctx, ip, local, buildSpcRestoreScript(target, backup))
	if err != nil {
		state.RestoreState = "failed"
		state.RestoreError, state.ErrorDetail = classifySpcCommandError(err, spcJoinDiag(output, errOut), spcStepRestore)
		return state
	}
	switch {
	case spcMarkerHas(output, "SPC_RESTORE_OK"):
		state.RestoreState = "restored"
	case spcMarkerHas(output, "SPC_RESTORE_SKIP"):
		state.RestoreState = "skipped"
	default:
		state.RestoreState = "failed"
		state.RestoreError = firstNonEmpty(spcMarkerField(output, "reason"), spcErrWrite)
		// 合并 stderr：只读挂载、权限不足等真实原因只在 stderr 里，
		// 只取 stdout 会只剩一个 copy_failed，与 prepare/write 分支同一教训。
		state.ErrorDetail = spcOutputTail(spcJoinDiag(output, errOut), 2, 200)
	}
	return state
}

func spcCountRestore(results []spcHostState) (int, int) {
	restored, failed := 0, 0
	for _, host := range results {
		switch host.RestoreState {
		case "restored":
			restored++
		case "failed":
			failed++
		}
	}
	return restored, failed
}

// watchSpcRestore 在后台等到任务终态后恢复原件。
//
// 为什么不把恢复完全交给平台：平台侧的轮询只发生在页面打开时，操作员一关页面
// 就没人收尾；bridge 是常驻进程，是"节点文件状态"最合适的责任方。
// 平台侧仍保留 restore_spc 作为重试入口，两条路都幂等。
func watchSpcRestore(b *app.Bridge, s *sshx.SSHSession, st *spcTaskState, traceID, caseID, nodeIP, customUI string) {
	go func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				logpush.Blog("ERROR", "log_collect.spc_restore_panic", "场景恢复协程异常退出", traceID, caseID, nodeIP, customUI, map[string]any{
					"task_id": st.TaskID, "panic": fmt.Sprint(recovered),
				})
			}
		}()
		deadline := time.Now().Add(spcRestoreMaxWait)
		idlePolls, failPolls := 0, 0
		for {
			time.Sleep(spcRestorePollInterval)
			if s == nil || s.IsClosed() {
				restoreSpcTask(b, s, st, "session_closed", traceID, caseID, nodeIP, customUI)
				return
			}
			// 采集期间键盘零输入，不打戳会被 TMOUT=600 踢掉（与 create/progress 同因）。
			s.MarkBusy()
			pollCtx, cancel := context.WithTimeout(context.Background(), logCollectProgressTimeout)
			output, exitCode, err := s.RunQuickCommand(pollCtx, logCollectAcliInvoke("log_collect progress"))
			cancel()
			if err != nil || exitCode != 0 {
				failPolls++
				if failPolls >= spcRestoreFailPolls {
					logpush.Blog("WARN", "log_collect.spc_restore_abandon", "进度连续不可用，恢复交给平台兜底", traceID, caseID, nodeIP, customUI, map[string]any{
						"task_id": st.TaskID, "error": errString(err), "exit_code": exitCode,
					})
					return
				}
				continue
			}
			failPolls = 0
			progress := parseLogCollectProgress(output)
			nodeState := classifyLogCollectNodeState(progress)
			st.noteState(nodeState)
			if nodeState == logCollectNodeNone {
				idlePolls++
			} else {
				idlePolls = 0
			}
			if due, reason := spcRestoreDue(nodeState, st.hasRun(), idlePolls); due {
				restoreSpcTask(b, s, st, reason, traceID, caseID, nodeIP, customUI)
				return
			}
			if time.Now().After(deadline) {
				restoreSpcTask(b, s, st, "timeout", traceID, caseID, nodeIP, customUI)
				return
			}
		}
	}()
}

// restoreSpcTask 执行一次恢复并落日志；beginRestore 保证同任务只跑一次。
func restoreSpcTask(b *app.Bridge, s *sshx.SSHSession, st *spcTaskState, reason, traceID, caseID, nodeIP, customUI string) {
	if st == nil || !st.beginRestore() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
	defer cancel()
	results := restoreSpcHosts(b, ctx, s, spcRestorePlan{
		Slot: st.Slot, Slots: st.Slots, TaskID: st.TaskID, Targets: st.Targets,
		LocalIPs: st.LocalIPs, Dirs: st.dirs(),
	})
	restored, failed := spcCountRestore(results)
	status := "success"
	level := "INFO"
	if failed > 0 {
		status = "failed"
		level = "WARN"
	}
	st.finishRestore(status, reason, results)
	// 还原全部成功才消费意图；失败保留，等下次重连恢复或显式 restore_spc 重试。
	// 提前删意图会把"还没还原完"伪装成"已收尾"，崩溃后没人再管残留清单。
	if failed == 0 {
		if err := reconcile.DeleteIntent(reconcile.KindSpcSlot, st.TaskID); err != nil {
			logpush.Blog("WARN", "log_collect.spc_intent_delete_failed", "还原成功但意图清理失败", traceID, caseID, nodeIP, customUI, map[string]any{
				"task_id": st.TaskID, "error": err.Error(),
			})
		}
	}
	logpush.Blog(level, "log_collect.spc_restore", "场景槽位恢复结果", traceID, caseID, nodeIP, customUI, map[string]any{
		"task_id": st.TaskID, "slot": st.Slot, "trigger": reason,
		"restored": restored, "failed": failed, "hosts_count": len(results),
		"hosts": spcHostAuditTrail(results),
	})
}

// handleSpcRestore 处理平台显式发起的 restore_spc。
//
// 现场可能已经不在内存里（bridge 重启过）：仍按「探测目录 + 拼备份名」恢复，
// 备份不存在时返回 skipped，绝不把"没有备份"说成"已恢复"。
func handleSpcRestore(b *app.Bridge, ctx context.Context, s *sshx.SSHSession, msg models.InMessage) (map[string]any, string, string) {
	taskID := strings.TrimSpace(msg.ExecID)
	if !spcTaskIDRe.MatchString(taskID) {
		return nil, "invalid_task_id", "缺少合法的任务 ID，无法定位备份"
	}
	slot := strings.TrimSpace(msg.SpcSlot)
	if slot == "" {
		slot = logCollectSpcSlot
	}
	state := spcTaskStore.get(taskID)
	// 内存现场丢了（bridge 重启过）：意图账本就是抗崩溃的后备现场，
	// 还原范围/本机地址/目录都从它取，不再依赖平台恰好传对 IP。
	var intent *reconcile.RestoreIntent
	if state == nil {
		intent = findSpcRestoreIntent(taskID)
	}
	slots := append([]string(nil), msg.SpcSlots...)
	if len(slots) == 0 && state != nil {
		slots = append(slots, state.Slots...)
	}
	if len(slots) == 0 && intent != nil {
		slots = append(slots, intent.Slots...)
	}
	if len(slots) == 0 {
		if slot != logCollectSpcSlot {
			return nil, "spc_slot_unsupported", fmt.Sprintf("不支持的 spc 槽位：%s", slot)
		}
	} else {
		for _, name := range slots {
			if !spcSlotRe.MatchString(name) || !logCollectModules[name] {
				return nil, "spc_slot_unsupported", fmt.Sprintf("不支持的 spc 槽位：%s", name)
			}
		}
		slot = slots[0]
	}
	plan := spcRestorePlan{Slot: slot, Slots: slots, TaskID: taskID}
	if state != nil {
		plan.Targets = state.Targets
		plan.LocalIPs = state.LocalIPs
		plan.Dirs = state.dirs()
	} else if intent != nil {
		plan.Targets = intent.NodeIPs
		plan.LocalIPs = intent.LocalIPs
		plan.Dirs = intent.Dirs
	}
	if len(plan.Targets) == 0 {
		var hosts []logCollectHostOption
		if value := strings.TrimSpace(msg.IPs); value == "" || strings.EqualFold(value, "all") {
			hosts = spcNodeList(s, ctx)
		}
		targets, _, err := spcTargetSet(msg.IPs, hosts)
		if err != nil {
			return nil, "spc_hosts_unknown", err.Error()
		}
		plan.Targets = targets
		plan.LocalIPs = spcLocalAddresses(s, ctx)
	}
	results := restoreSpcHosts(b, ctx, s, plan)
	restored, failed := spcCountRestore(results)
	status := "success"
	if failed > 0 {
		status = "failed"
	}
	if state != nil {
		state.finishRestore(status, "", results)
	}
	if failed == 0 {
		// 幂等消费：意图不存在时 DeleteIntent 也返回成功（重启后显式还原同样收尾）。
		_ = reconcile.DeleteIntent(reconcile.KindSpcSlot, taskID)
	}
	logData := map[string]any{
		"task_id":        taskID,
		"spc_slot":       slot,
		"spc_slots":      slots,
		"restore_status": status,
		"spc_hosts":      spcPersistHosts(results),
		"restored_count": restored,
		"failed_count":   failed,
	}
	if failed > 0 {
		return logData, "spc_restore_failed", fmt.Sprintf("%d 台节点未能恢复原清单，需人工核对节点上的备份文件", failed)
	}
	return logData, "", ""
}

// persistSpcRestoreIntent 把终态还原意图落盘（§3.2 抗崩溃三件套的第四件）。
// watcher 是内存协程，bridge 被杀后还原责任必须能从盘上找回。
// 意图内容是产品语义（哪些节点/槽位/目录），由消费者给；骨架只管形状与读写。
// 落盘失败不阻断下发：内存 watcher 与显式 restore_spc 仍是两条活路，
// 但必须留痕——静默丢意图会把抗崩溃变成运气。
func persistSpcRestoreIntent(b *app.Bridge, st *spcTaskState, traceID, caseID, nodeIP, customUI string) {
	slots := st.Slots
	if len(slots) == 0 {
		slots = []string{st.Slot}
	}
	intent := reconcile.RestoreIntent{
		Kind: reconcile.KindSpcSlot, TaskKey: st.TaskID, WaitFor: reconcile.WaitTaskTerminal,
		// 只登记真正写成功的节点：从未写入的节点没有需要还原的改动。
		NodeIPs: st.writtenTargets(), LocalIPs: st.LocalIPs, Dirs: st.dirs(),
		Slots: slots, CreatedAt: time.Now(), Reason: "custom_scenario",
	}
	if err := reconcile.SaveIntent(intent); err != nil {
		logpush.Blog("WARN", "log_collect.spc_intent_save_failed", "还原意图落盘失败，崩溃后需显式 restore_spc 兜底", traceID, caseID, nodeIP, customUI, map[string]any{
			"task_id": st.TaskID, "error": err.Error(),
		})
		return
	}
	superseded := supersedeSpcRestoreIntents(intent)
	logpush.Blog("INFO", "log_collect.spc_intent_persisted", "终态还原意图已落盘", traceID, caseID, nodeIP, customUI, map[string]any{
		"task_id": st.TaskID, "hosts_count": len(intent.NodeIPs), "slots": slots, "superseded": superseded,
	})
}

// supersedeSpcRestoreIntents 删除与本次任务在"主机×槽位"上相交的旧意图。
// 原因：本次写入前 prepare 的写前自愈已把旧任务残留的原件放回去，
// 场景所有权转移到本次任务；旧意图若留着，bridge 重启后的恢复会
// 把旧原件盖到新任务的平台清单上——而采集可能正在跑。
func supersedeSpcRestoreIntents(current reconcile.RestoreIntent) int {
	intents, err := reconcile.LoadIntents()
	if err != nil {
		return 0
	}
	hosts := map[string]bool{}
	for _, ip := range current.NodeIPs {
		hosts[ip] = true
	}
	slots := map[string]bool{}
	for _, slot := range current.Slots {
		slots[slot] = true
	}
	deleted := 0
	for _, old := range intents {
		if old.Kind != current.Kind || old.TaskKey == current.TaskKey {
			continue
		}
		hostOverlap, slotOverlap := false, false
		for _, ip := range old.NodeIPs {
			if hosts[ip] {
				hostOverlap = true
				break
			}
		}
		for _, slot := range old.Slots {
			if slots[slot] {
				slotOverlap = true
				break
			}
		}
		if hostOverlap && slotOverlap {
			if reconcile.DeleteIntent(old.Kind, old.TaskKey) == nil {
				deleted++
			}
		}
	}
	return deleted
}

// findSpcRestoreIntent 按任务键查意图；读不到/损坏都返回 nil，
// 调用方退回"探测目录 + 拼备份名"的既有兜底路径。
func findSpcRestoreIntent(taskID string) *reconcile.RestoreIntent {
	intents, err := reconcile.LoadIntents()
	if err != nil {
		return nil
	}
	for index := range intents {
		if intents[index].Kind == reconcile.KindSpcSlot && intents[index].TaskKey == taskID {
			return &intents[index]
		}
	}
	return nil
}

// recoverSpcRestoreIntents 在会话（重）建立后消费持久还原意图（§3.2）。
// bridge 重启后 watcher 协程没了，但意图与自描述备份还在盘上：
// 这里凭意图把原件放回去，还原全部成功才删意图；失败保留，
// 等下次重连或显式 restore_spc 重试。必须由调用方放进协程：
// 集合性还原可能几十秒，不能阻塞 ssh_connect 回包。
func recoverSpcRestoreIntents(b *app.Bridge, s *sshx.SSHSession, traceID, caseID, nodeIP, customUI string) {
	intents, err := reconcile.LoadIntents()
	if err != nil {
		logpush.Blog("WARN", "log_collect.spc_intent_load_failed", "还原意图读取失败", traceID, caseID, nodeIP, customUI, map[string]any{
			"error": err.Error(),
		})
		return
	}
	for _, intent := range intents {
		if intent.Kind != reconcile.KindSpcSlot {
			continue
		}
		// 内存 watcher 还活着（同进程重连）：还原责任归它，不双头接管。
		// 两条路真同时跑也幂等（第二次 skipped），但跳过省掉无谓 SSH 往返。
		if spcTaskStore.get(intent.TaskKey) != nil {
			continue
		}
		if len(intent.NodeIPs) == 0 {
			_ = reconcile.DeleteIntent(intent.Kind, intent.TaskKey)
			continue
		}
		slot := logCollectSpcSlot
		if len(intent.Slots) > 0 {
			slot = intent.Slots[0]
		}
		// 还原期间键盘零输入，不打戳会被 TMOUT=600 踢掉（与 watcher 同因）。
		s.MarkBusy()
		ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
		results := restoreSpcHosts(b, ctx, s, spcRestorePlan{
			Slot: slot, Slots: intent.Slots, TaskID: intent.TaskKey,
			Targets: intent.NodeIPs, LocalIPs: intent.LocalIPs, Dirs: intent.Dirs,
		})
		cancel()
		restored, failed := spcCountRestore(results)
		if failed > 0 {
			logpush.Blog("WARN", "log_collect.spc_intent_recover_failed", "重启后还原未全部成功，意图保留待重试", traceID, caseID, nodeIP, customUI, map[string]any{
				"task_id": intent.TaskKey, "restored": restored, "failed": failed,
				"hosts": spcHostAuditTrail(results),
			})
			continue
		}
		if err := reconcile.DeleteIntent(intent.Kind, intent.TaskKey); err != nil {
			logpush.Blog("WARN", "log_collect.spc_intent_delete_failed", "还原成功但意图清理失败", traceID, caseID, nodeIP, customUI, map[string]any{
				"task_id": intent.TaskKey, "error": err.Error(),
			})
		}
		logpush.Blog("INFO", "log_collect.spc_intent_recovered", "重启后凭持久意图完成终态还原", traceID, caseID, nodeIP, customUI, map[string]any{
			"task_id": intent.TaskKey, "restored": restored, "hosts_count": len(results),
			"hosts": spcHostAuditTrail(results),
		})
	}
}
