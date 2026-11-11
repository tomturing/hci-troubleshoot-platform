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
	"encoding/base64"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
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
const (
	spcErrUnreachable  = "host_unreachable"
	spcErrDirMissing   = "spc_dir_missing"
	spcErrDirAmbiguous = "spc_dir_ambiguous"
	spcErrFileMissing  = "spc_file_missing"
	spcErrBackup       = "backup_failed"
	spcErrWrite        = "write_failed"
)

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
type spcRejection struct {
	ErrType string
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

// buildSpcDirCommand 探测 spc 目录；已知目录时先做存在性校验，避免复用一个已失效的缓存。
func buildSpcDirCommand(hint string) string {
	if strings.TrimSpace(hint) == "" {
		return buildSpcFindCommand()
	}
	return fmt.Sprintf("[ -d %s ] && printf '%%s\\n' %s || %s", shellQuote(hint), shellQuote(hint), buildSpcFindCommand())
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

// buildSpcPrepareCommand 完成「写前自愈 + 备份 + 备份保真校验」。
//
// 自愈顺序不可颠倒：先把历史备份（原件）放回去，再备份，否则本次备份存下的是
// 上一次的平台清单，原件会被永久覆盖。本次任务的备份已存在时不再覆盖，
// 保证重试写同一个任务不会把原件换成平台内容。
func buildSpcPrepareCommand(target, backup string) string {
	glob := fmt.Sprintf("%s.platform.*.bak", shellQuote(target))
	prev := fmt.Sprintf("__prev=$(ls -1t %s 2>/dev/null | grep -v -F %s | head -n 1)", glob, shellQuote(backup))
	selfHeal := fmt.Sprintf("if [ -n \"$__prev\" ]; then cp -p \"$__prev\" %s 2>/dev/null; fi", shellQuote(target))
	// 校验只在"本次刚创建备份"的同一分支里做：同一任务重试时备份已存在，
	// 此时 target 可能就是上一轮写入的平台清单，再比对必然失败并误判环境异常。
	backupCmd := fmt.Sprintf("if [ ! -f %s ]; then cp -p %s %s 2>/dev/null || { printf 'SPC_PREPARE_FAIL reason=backup_failed\\n'; exit 0; }; "+
		"cmp -s %s %s || { printf 'SPC_PREPARE_FAIL reason=backup_mismatch\\n'; exit 0; }; fi",
		shellQuote(backup), shellQuote(target), shellQuote(backup), shellQuote(target), shellQuote(backup))
	orig := fmt.Sprintf("__orig=$(sha256sum %s 2>/dev/null | awk '{print $1}')", shellQuote(backup))
	return fmt.Sprintf("%s; %s; %s; %s; printf 'SPC_PREPARE_OK orig=%%s\\n' \"$__orig\"", prev, selfHeal, backupCmd, orig)
}

// buildSpcWriteVerifyCommand 写入并回读校验，任一步失败立刻用备份回滚。
//
// 校验用 base64 逐字节比对而不是 sha256：base64 是 coreutils 必备命令，
// 而 sha256sum 在裁剪系统上可能缺失，缺了就等于"写完没有证据"。
func buildSpcWriteVerifyCommand(target, backup, content string) string {
	write := buildSpcWriteCommand(target, content)
	verify := fmt.Sprintf("[ \"$(base64 %s 2>/dev/null | tr -d '\\n')\" = %s ]", shellQuote(target), shellQuote(spcBase64(content)))
	rollback := buildSpcRestoreCommand(backup, target)
	return fmt.Sprintf("%s || { %s 2>/dev/null; printf 'SPC_WRITE_FAIL reason=write_rejected\\n'; exit 0; }; "+
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

// runSpcOnHost 在目标主机上执行一段脚本：本机直连当前会话，其余节点走一次 ssh。
func (s *SSHSession) runSpcOnHost(ctx context.Context, ip string, local bool, script string) (string, int, error) {
	command := script
	if !local {
		command = buildSpcSSHCommand(ip, script)
	}
	hostCtx, cancel := context.WithTimeout(ctx, spcHostCommandTimeout)
	defer cancel()
	return s.runQuickCommand(hostCtx, command)
}

// spcNodeList 取集群节点清单（与 probe 同源命令），用于把 all 解析成具体主机集合。
func (s *SSHSession) spcNodeList(ctx context.Context) []logCollectHostOption {
	probeCtx, cancel := context.WithTimeout(ctx, spcHostCommandTimeout)
	defer cancel()
	output, _, err := s.runCommandPty(probeCtx, logCollectAcliInvoke("--formatter json platform node list"), spcHostCommandTimeout)
	if err != nil {
		return nil
	}
	return parseLogCollectNodeList(output)
}

// spcLocalAddresses 探测本机 IPv4；探测失败返回空集合（等价于全部走 ssh）。
func (s *SSHSession) spcLocalAddresses(ctx context.Context) map[string]bool {
	probeCtx, cancel := context.WithTimeout(ctx, spcHostCommandTimeout)
	defer cancel()
	output, _, err := s.runQuickCommand(probeCtx, buildSpcLocalAddressCommand())
	if err != nil {
		return map[string]bool{}
	}
	return parseSpcLocalAddresses(output)
}

func spcBase64(raw string) string {
	return base64.StdEncoding.EncodeToString([]byte(raw))
}

// parseSpcFiles 校验平台签发的逐槽位清单。
// 槽位必须是 acli 合法模块、内容必须过 spc 二级校验、摘要必须自洽：
// 任何一项不合规都拒绝下发，绝不带着半份清单去改节点文件。
func parseSpcFiles(msg InMessage) ([]spcSlotFile, *spcRejection) {
	if len(msg.SpcFiles) == 0 {
		return nil, nil
	}
	if strings.TrimSpace(msg.SpcJSON) != "" {
		return nil, &spcRejection{"spc_config_conflict", "spc_files 与 spc_json 不能同时下发"}
	}
	seen := map[string]bool{}
	files := make([]spcSlotFile, 0, len(msg.SpcFiles))
	for _, raw := range msg.SpcFiles {
		slot := strings.TrimSpace(raw.Slot)
		if !spcSlotRe.MatchString(slot) || !logCollectModules[slot] {
			return nil, &spcRejection{"invalid_spc", fmt.Sprintf("不支持的清单槽位：%s", slot)}
		}
		if seen[slot] {
			return nil, &spcRejection{"invalid_spc", fmt.Sprintf("清单槽位重复：%s", slot)}
		}
		seen[slot] = true
		if len(raw.Content) == 0 || len(raw.Content) > logCollectSpcMaxBytes {
			return nil, &spcRejection{"invalid_spc", fmt.Sprintf("槽位 %s 的清单为空或超过 %d 字节", slot, logCollectSpcMaxBytes)}
		}
		if err := validateSpcContent(raw.Content); err != nil {
			return nil, &spcRejection{"invalid_spc", fmt.Sprintf("槽位 %s 的清单非法：%v", slot, err)}
		}
		digest := spcSHA256(raw.Content)
		if declared := strings.ToLower(strings.TrimSpace(raw.SHA256)); declared != "" && declared != digest {
			return nil, &spcRejection{"spc_digest_mismatch", fmt.Sprintf("槽位 %s 的清单摘要与内容不一致（传输可能被截断或改写）", slot)}
		}
		files = append(files, spcSlotFile{Slot: slot, Content: raw.Content, SHA256: digest})
	}
	return files, nil
}

// prepareSpcScenario 完成「解析目标主机 + 逐台逐槽位写入 + 全量校验」，成功才返回现场。
//
// 任何一台、任一槽位写不进去都回滚并拒绝：宁可这一次不发任务，也不能让节点之间
// 用不同的清单收集——那种结果事后无法从采集结果里看出来。
func (b *Bridge) prepareSpcScenario(ctx context.Context, s *SSHSession, msg InMessage, traceID, customUI string) (*spcTaskState, *spcRejection) {
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
		return nil, &spcRejection{"spc_slot_unsupported", fmt.Sprintf("不支持的 spc 槽位：%s（bridge 仅支持 %s）", slot, logCollectSpcSlot)}
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
			return nil, &spcRejection{"invalid_spc", fmt.Sprintf("场景内容非法：%v", err)}
		}
		digest = spcSHA256(msg.SpcJSON)
		if declared := strings.ToLower(strings.TrimSpace(msg.SpcSHA256)); declared != "" && declared != digest {
			return nil, &spcRejection{"spc_digest_mismatch", "场景摘要与内容不一致（传输可能被截断或改写）"}
		}
		spec.Content = msg.SpcJSON
		spcBytes = len(msg.SpcJSON)
	}

	taskID := strings.TrimSpace(msg.ExecID)
	if !spcTaskIDRe.MatchString(taskID) {
		return nil, &spcRejection{"invalid_task_id", "缺少合法的任务 ID，无法确定备份归属"}
	}
	spec.TaskID = taskID

	var hosts []logCollectHostOption
	if value := strings.TrimSpace(msg.IPs); value == "" || strings.EqualFold(value, "all") {
		hosts = s.spcNodeList(ctx)
	}
	targets, all, err := spcTargetSet(msg.IPs, hosts)
	if err != nil {
		return nil, &spcRejection{"spc_hosts_unknown", err.Error()}
	}

	state := &spcTaskState{
		TaskID:       taskID,
		Slot:         slot,
		Slots:        slots,
		Targets:      targets,
		LocalIPs:     s.spcLocalAddresses(ctx),
		CreatedAt:    time.Now(),
		restoreState: "pending",
	}
	blog("INFO", "log_collect.spc_fanout_start", "开始把场景写入目标节点", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
		"task_id": taskID, "slot": slot, "slots": slots, "spc_sha256": digest,
		"spc_digests": digests, "hosts_count": len(targets), "ips_all": all, "spc_bytes": spcBytes,
	})

	results := b.applySpcAll(ctx, s, state, spec)
	state.setHosts(results)

	written, broken, unreachable := spcClassifyHosts(results)
	// 环境坏（目录缺失/多份/备份失败/写入失败）必须停手；
	// 不可达只在"用户点名了这几台"时算失败，all 模式下视为本轮不参与。
	if len(broken) > 0 || (!all && len(unreachable) > 0) || written == 0 {
		rejection := &spcRejection{"spc_write_failed", spcRejectionMessage(written, broken, unreachable)}
		restored, restoreFailed := b.rollbackSpcAll(s, state, "写入未通过，已回滚")
		blog("ERROR", "log_collect.spc_fanout_failed", "场景写入未通过，已回滚并拒绝下发", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
			"task_id": taskID, "slot": slot, "written": written,
			"broken_hosts": broken, "unreachable_hosts": unreachable,
			"restored_hosts": restored, "restore_failed_hosts": restoreFailed,
			"reason": rejection.Message,
		})
		return nil, rejection
	}

	spcTaskStore.put(state)
	blog("INFO", "log_collect.spc_fanout", "场景已写入全部目标节点", traceID, msg.CaseID, msg.NodeIP, customUI, map[string]any{
		"task_id": taskID, "slot": slot, "spc_sha256": digest,
		"written": written, "excluded_hosts": unreachable,
		"hosts": spcHostAuditTrail(results),
	})
	return state, nil
}

// spcClassifyHosts 把逐台结果拆成「已写入 / 环境坏 / 不可达」。
func spcClassifyHosts(results []spcHostState) (int, []string, []string) {
	written := 0
	broken := make([]string, 0, 2)
	unreachable := make([]string, 0, 2)
	for _, host := range results {
		if host.Written {
			written++
			continue
		}
		if host.ErrorCode == spcErrUnreachable {
			unreachable = append(unreachable, host.IP)
			continue
		}
		broken = append(broken, fmt.Sprintf("%s(%s)", host.IP, firstNonEmpty(host.ErrorCode, spcErrWrite)))
	}
	sort.Strings(broken)
	sort.Strings(unreachable)
	return written, broken, unreachable
}

// spcRejectionMessage 拼面向现场的一句话原因，必须带上具体主机与根因码，
// 否则用户只能看到"写入失败"而不知道该去查哪台。
func spcRejectionMessage(written int, broken, unreachable []string) string {
	if written == 0 && len(broken) == 0 && len(unreachable) == 0 {
		return "没有任何节点写入成功"
	}
	parts := make([]string, 0, 3)
	if len(broken) > 0 {
		parts = append(parts, "节点环境异常："+strings.Join(broken, "、"))
	}
	if len(unreachable) > 0 {
		parts = append(parts, "节点不可达："+strings.Join(unreachable, "、"))
	}
	if written == 0 {
		parts = append(parts, "可用节点为 0")
	}
	return truncateString("场景未能写入全部目标节点（"+strings.Join(parts, "；")+"）", 400)
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

// applySpcAll 并发把场景写到全部目标节点。
func (b *Bridge) applySpcAll(ctx context.Context, s *SSHSession, state *spcTaskState, spec spcApplySpec) []spcHostState {
	results := make([]spcHostState, len(state.Targets))
	sem := make(chan struct{}, spcFanoutConcurrency)
	var wg sync.WaitGroup
	for index, ip := range state.Targets {
		wg.Add(1)
		go func(index int, ip string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			results[index] = b.applySpcToHost(ctx, s, spec, ip, state.LocalIPs[ip])
		}(index, ip)
	}
	wg.Wait()
	return results
}

// applySpcToHost 在单台主机上完成目录探测 → 逐槽位备份（含自愈）→ 写入 → 回读校验。
//
// 多槽位下任一槽位失败即把整台标为失败：写不全就绝不能下发，
// 否则部分模块用平台清单、部分用节点原清单，产物无法解释。
func (b *Bridge) applySpcToHost(ctx context.Context, s *SSHSession, spec spcApplySpec, ip string, local bool) spcHostState {
	state := spcHostState{IP: ip, RestoreState: "pending"}

	output, _, err := s.runSpcOnHost(ctx, ip, local, buildSpcDirCommand(""))
	if err != nil {
		state.ErrorCode = spcErrUnreachable
		state.ErrorDetail = errString(err)
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
		slotState := b.applySpcSlotToHost(ctx, s, ip, local, dir, spec.TaskID, file)
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
func (b *Bridge) applySpcSlotToHost(ctx context.Context, s *SSHSession, ip string, local bool, dir, taskID string, file spcSlotFile) spcHostSlotState {
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

	prepareOut, _, prepareErr := s.runSpcOnHost(ctx, ip, local, buildSpcPrepareCommand(target, backup))
	if prepareErr != nil {
		state.ErrorCode = spcErrUnreachable
		state.ErrorDetail = errString(prepareErr)
		return state
	}
	if !spcMarkerHas(prepareOut, "SPC_PREPARE_OK") {
		state.ErrorCode = firstNonEmpty(spcMarkerField(prepareOut, "reason"), spcErrBackup)
		return state
	}
	state.OriginalSHA = spcMarkerField(prepareOut, "orig")

	writeOut, _, writeErr := s.runSpcOnHost(ctx, ip, local, buildSpcWriteVerifyCommand(target, backup, file.Content))
	if writeErr != nil {
		state.ErrorCode = spcErrUnreachable
		state.ErrorDetail = errString(writeErr)
		return state
	}
	if !spcMarkerHas(writeOut, "SPC_WRITE_OK") {
		state.ErrorCode = firstNonEmpty(spcMarkerField(writeOut, "reason"), spcErrWrite)
		return state
	}
	state.Written = true
	return state
}

// rollbackSpcAll 回滚本次写入（全量恢复，备份不存在的节点自动 skip）。
// 返回 (恢复成功台数, 恢复失败台数)。
func (b *Bridge) rollbackSpcAll(s *SSHSession, st *spcTaskState, reason string) (int, int) {
	ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
	defer cancel()
	results := b.restoreSpcHosts(ctx, s, spcRestorePlan{
		Slot: st.Slot, Slots: st.Slots, TaskID: st.TaskID, Targets: st.Targets,
		LocalIPs: st.LocalIPs, Dirs: st.dirs(),
	})
	restored, failed := spcCountRestore(results)
	status := "success"
	if failed > 0 {
		status = "failed"
	}
	st.finishRestore(status, reason, results)
	return restored, failed
}

// restoreSpcHosts 并发恢复全部目标节点。
func (b *Bridge) restoreSpcHosts(ctx context.Context, s *SSHSession, plan spcRestorePlan) []spcHostState {
	results := make([]spcHostState, len(plan.Targets))
	sem := make(chan struct{}, spcFanoutConcurrency)
	var wg sync.WaitGroup
	for index, ip := range plan.Targets {
		wg.Add(1)
		go func(index int, ip string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			results[index] = b.restoreSpcOnHost(ctx, s, plan, ip)
		}(index, ip)
	}
	wg.Wait()
	return results
}

func (b *Bridge) restoreSpcOnHost(ctx context.Context, s *SSHSession, plan spcRestorePlan, ip string) spcHostState {
	state := spcHostState{IP: ip}
	local := plan.LocalIPs[ip]
	dir := plan.Dirs[ip]
	if dir == "" {
		output, _, err := s.runSpcOnHost(ctx, ip, local, buildSpcDirCommand(""))
		if err != nil {
			state.RestoreState = "failed"
			state.RestoreError = spcErrUnreachable
			return state
		}
		resolved, code := spcResolveDir(output)
		if code != "" {
			state.RestoreState = "failed"
			state.RestoreError = code
			return state
		}
		dir = resolved
	}
	state.Dir = dir

	slots := plan.Slots
	if len(slots) == 0 {
		slots = []string{plan.Slot}
	}
	restored, skipped, failed := 0, 0, 0
	firstErr := ""
	for _, slot := range slots {
		slotState := b.restoreSpcSlotOnHost(ctx, s, ip, local, dir, plan.TaskID, slot)
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
func (b *Bridge) restoreSpcSlotOnHost(ctx context.Context, s *SSHSession, ip string, local bool, dir, taskID, slot string) spcHostSlotState {
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

	output, _, err := s.runSpcOnHost(ctx, ip, local, buildSpcRestoreScript(target, backup))
	if err != nil {
		state.RestoreState = "failed"
		state.RestoreError = spcErrUnreachable
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
func (b *Bridge) watchSpcRestore(s *SSHSession, st *spcTaskState, traceID, caseID, nodeIP, customUI string) {
	go func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				blog("ERROR", "log_collect.spc_restore_panic", "场景恢复协程异常退出", traceID, caseID, nodeIP, customUI, map[string]any{
					"task_id": st.TaskID, "panic": fmt.Sprint(recovered),
				})
			}
		}()
		deadline := time.Now().Add(spcRestoreMaxWait)
		idlePolls, failPolls := 0, 0
		for {
			time.Sleep(spcRestorePollInterval)
			if s == nil || s.isClosed() {
				b.restoreSpcTask(s, st, "session_closed", traceID, caseID, nodeIP, customUI)
				return
			}
			// 采集期间键盘零输入，不打戳会被 TMOUT=600 踢掉（与 create/progress 同因）。
			s.markBusy()
			pollCtx, cancel := context.WithTimeout(context.Background(), logCollectProgressTimeout)
			output, exitCode, err := s.runQuickCommand(pollCtx, logCollectAcliInvoke("log_collect progress"))
			cancel()
			if err != nil || exitCode != 0 {
				failPolls++
				if failPolls >= spcRestoreFailPolls {
					blog("WARN", "log_collect.spc_restore_abandon", "进度连续不可用，恢复交给平台兜底", traceID, caseID, nodeIP, customUI, map[string]any{
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
				b.restoreSpcTask(s, st, reason, traceID, caseID, nodeIP, customUI)
				return
			}
			if time.Now().After(deadline) {
				b.restoreSpcTask(s, st, "timeout", traceID, caseID, nodeIP, customUI)
				return
			}
		}
	}()
}

// restoreSpcTask 执行一次恢复并落日志；beginRestore 保证同任务只跑一次。
func (b *Bridge) restoreSpcTask(s *SSHSession, st *spcTaskState, reason, traceID, caseID, nodeIP, customUI string) {
	if st == nil || !st.beginRestore() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), spcRestoreActionBudget)
	defer cancel()
	results := b.restoreSpcHosts(ctx, s, spcRestorePlan{
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
	blog(level, "log_collect.spc_restore", "场景槽位恢复结果", traceID, caseID, nodeIP, customUI, map[string]any{
		"task_id": st.TaskID, "slot": st.Slot, "trigger": reason,
		"restored": restored, "failed": failed, "hosts_count": len(results),
		"hosts": spcHostAuditTrail(results),
	})
}

// handleSpcRestore 处理平台显式发起的 restore_spc。
//
// 现场可能已经不在内存里（bridge 重启过）：仍按「探测目录 + 拼备份名」恢复，
// 备份不存在时返回 skipped，绝不把"没有备份"说成"已恢复"。
func (b *Bridge) handleSpcRestore(ctx context.Context, s *SSHSession, msg InMessage) (map[string]any, string, string) {
	taskID := strings.TrimSpace(msg.ExecID)
	if !spcTaskIDRe.MatchString(taskID) {
		return nil, "invalid_task_id", "缺少合法的任务 ID，无法定位备份"
	}
	slot := strings.TrimSpace(msg.SpcSlot)
	if slot == "" {
		slot = logCollectSpcSlot
	}
	state := spcTaskStore.get(taskID)
	slots := append([]string(nil), msg.SpcSlots...)
	if len(slots) == 0 && state != nil {
		slots = append(slots, state.Slots...)
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
	}
	if len(plan.Targets) == 0 {
		var hosts []logCollectHostOption
		if value := strings.TrimSpace(msg.IPs); value == "" || strings.EqualFold(value, "all") {
			hosts = s.spcNodeList(ctx)
		}
		targets, _, err := spcTargetSet(msg.IPs, hosts)
		if err != nil {
			return nil, "spc_hosts_unknown", err.Error()
		}
		plan.Targets = targets
		plan.LocalIPs = s.spcLocalAddresses(ctx)
	}
	results := b.restoreSpcHosts(ctx, s, plan)
	restored, failed := spcCountRestore(results)
	status := "success"
	if failed > 0 {
		status = "failed"
	}
	if state != nil {
		state.finishRestore(status, "", results)
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
