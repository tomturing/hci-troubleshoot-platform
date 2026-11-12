package main

// spc 扇出编排层单测：单片原语见 log_collect_spc_test.go，这里只覆盖"决定写谁、
// 写完怎么证明、什么时候还原、还原状态怎么表达"这些会导致静默错采的判定。

import (
	"strings"
	"testing"
)

func TestSpcTargetSet(t *testing.T) {
	hosts := []logCollectHostOption{{IP: "10.0.0.3"}, {IP: "10.0.0.2"}, {IP: " 10.0.0.2 "}, {IP: ""}}

	t.Run("all 无节点清单必须拒绝", func(t *testing.T) {
		// 拿不到清单时若退化成"只写发起节点"，其余节点会静默按原清单收集，
		// 正是本功能要消灭的失败模式，所以这里必须是错误而不是空集合。
		for _, value := range []string{"", "all", " ALL "} {
			got, all, err := spcTargetSet(value, nil)
			if err == nil {
				t.Fatalf("%q 缺少节点清单却未报错：%v", value, got)
			}
			if !all {
				t.Fatalf("%q 应被识别为 all 语义", value)
			}
		}
	})

	t.Run("all 解析清单并去重排序", func(t *testing.T) {
		got, all, err := spcTargetSet("all", hosts)
		if err != nil || !all {
			t.Fatalf("意外失败：%v all=%v", err, all)
		}
		want := []string{"10.0.0.2", "10.0.0.3"}
		if !sameStrings(got, want) {
			t.Fatalf("目标集合 = %v，期望 %v", got, want)
		}
	})

	t.Run("显式 IP 排序去重且非 all", func(t *testing.T) {
		got, all, err := spcTargetSet("10.0.0.9, 10.0.0.1,10.0.0.9", nil)
		if err != nil || all {
			t.Fatalf("意外失败：%v all=%v", err, all)
		}
		if want := []string{"10.0.0.1", "10.0.0.9"}; !sameStrings(got, want) {
			t.Fatalf("目标集合 = %v，期望 %v", got, want)
		}
	})

	t.Run("非法 IP 一律拒绝", func(t *testing.T) {
		for _, value := range []string{"1.2.3.999", "1.2.3", "10.0.0.1;rm -rf /", "  "} {
			if _, _, err := spcTargetSet(value, nil); err == nil {
				t.Fatalf("%q 应被拒绝", value)
			}
		}
	})
}

func TestSpcValidIPv4(t *testing.T) {
	cases := map[string]bool{
		"10.0.0.1":      true,
		"255.255.255.0": true,
		"0.0.0.0":       true,
		"256.1.1.1":     false,
		"1.2.3":         false,
		"01.2.3.4":      false, // 前导零会让 ssh 目标产生歧义
		"1.2.3.4 ":      false,
		"1.2.3.4\n":     false,
		"a.b.c.d":       false,
		"":              false,
	}
	for value, want := range cases {
		if got := spcValidIPv4(value); got != want {
			t.Fatalf("spcValidIPv4(%q) = %v，期望 %v", value, got, want)
		}
	}
}

// TestBuildSpcSSHCommandSecurity 守住两条硬约束：没有免密就立刻失败，
// 不可达节点必须 5s 内失败——否则一条命令就能吃掉整个 180s 动作预算。
func TestBuildSpcSSHCommandSecurity(t *testing.T) {
	command := buildSpcSSHCommand("10.0.0.1", "true")
	for _, must := range []string{"BatchMode=yes", "ConnectTimeout=5", "root@10.0.0.1"} {
		if !strings.Contains(command, must) {
			t.Fatalf("ssh 命令缺少 %q：%s", must, command)
		}
	}
}

// TestBuildSpcPrepareCommandOrder 自愈必须发生在备份之前，且备份保真校验
// 只能出现在"本次刚创建备份"的分支里（否则同一任务重试会被误判为环境异常）。
func TestBuildSpcPrepareCommandOrder(t *testing.T) {
	command := buildSpcPrepareCommand("/sf/x/spc/core.json", "/sf/x/spc/core.json.platform.task1.bak")

	if !strings.Contains(command, "SPC_PREPARE_OK") {
		t.Fatalf("缺少成功标记：%s", command)
	}
	selfHeal := strings.Index(command, "__prev")
	if selfHeal < 0 {
		t.Fatalf("缺少历史备份自愈：%s", command)
	}
	backupGate := strings.Index(command, "if [ ! -f ")
	if backupGate < 0 {
		t.Fatalf("缺少备份存在性判断：%s", command)
	}
	if selfHeal > backupGate {
		t.Fatalf("自愈必须早于备份，否则本次备份会存下上一轮的平台清单：%s", command)
	}
	verify := strings.Index(command, "cmp -s")
	if verify < backupGate {
		t.Fatalf("备份保真校验必须落在创建备份的分支内：%s", command)
	}
	// 校验的右操作数必须是备份本身，写反会变成"用平台内容证明平台内容"。
	if !strings.Contains(command, "cmp -s '/sf/x/spc/core.json' '/sf/x/spc/core.json.platform.task1.bak'") {
		t.Fatalf("备份保真校验方向不对：%s", command)
	}
}

// TestBuildSpcWriteVerifyCommand 写入必须可证明：base64 回读比对，失败就用备份回滚。
func TestBuildSpcWriteVerifyCommand(t *testing.T) {
	content := `[{"action":"copy","path":"/var/log/messages"}]`
	command := buildSpcWriteVerifyCommand("/sf/x/spc/core.json", "/sf/x/spc/core.json.platform.task1.bak", content)

	if !strings.Contains(command, "base64") {
		t.Fatalf("缺少 base64 回读校验：%s", command)
	}
	if strings.Contains(command, "sha256sum") {
		t.Fatalf("裁剪系统可能没有 sha256sum，不能作为唯一校验手段：%s", command)
	}
	for _, marker := range []string{"SPC_WRITE_OK", "reason=verify_mismatch", "reason=write_rejected"} {
		if !strings.Contains(command, marker) {
			t.Fatalf("缺少 %q：%s", marker, command)
		}
	}
	rollback := buildSpcRestoreCommand("/sf/x/spc/core.json.platform.task1.bak", "/sf/x/spc/core.json")
	if count := strings.Count(command, rollback); count != 2 {
		t.Fatalf("失败路径应各回滚一次，实际 %d 次：%s", count, command)
	}
}

// TestBuildSpcRestoreScriptIdempotent 备份不存在必须报 skip 而不是假装恢复成功。
func TestBuildSpcRestoreScriptIdempotent(t *testing.T) {
	command := buildSpcRestoreScript("/sf/x/spc/core.json", "/sf/x/spc/core.json.platform.task1.bak")
	for _, marker := range []string{"SPC_RESTORE_SKIP", "SPC_RESTORE_OK", "SPC_RESTORE_FAIL"} {
		if !strings.Contains(command, marker) {
			t.Fatalf("缺少 %q：%s", marker, command)
		}
	}
}

func TestSpcMarkerHelpers(t *testing.T) {
	output := "warning: something\r\nSPC_PREPARE_OK orig=abc123\r\nSPC_WRITE_FAIL reason=verify_mismatch\n"
	if !spcMarkerHas(output, "SPC_PREPARE_OK") {
		t.Fatal("应识别出成功标记")
	}
	if spcMarkerHas(output, "SPC_RESTORE_OK") {
		t.Fatal("不应识别出未出现的标记")
	}
	if got := spcMarkerField(output, "orig"); got != "abc123" {
		t.Fatalf("orig = %q", got)
	}
	if got := spcMarkerField(output, "reason"); got != "verify_mismatch" {
		t.Fatalf("reason = %q", got)
	}
	if got := spcMarkerField("no markers here", "orig"); got != "" {
		t.Fatalf("无标记时应返回空，得到 %q", got)
	}
}

// TestSpcRestoreDue 恢复时机是"会不会错采"的核心判定：
// 早一步会用原清单采，晚一步会让平台清单长期占着槽位。
func TestSpcRestoreDue(t *testing.T) {
	cases := []struct {
		name        string
		nodeState   string
		everRunning bool
		idlePolls   int
		wantDue     bool
		wantReason  string
	}{
		{"完成即恢复", logCollectNodeCompleted, true, 0, true, "task_completed"},
		{"异常结束也恢复", logCollectNodeAbnormal, true, 0, true, "task_abnormal"},
		{"跑过之后消失", logCollectNodeNone, true, 0, true, "task_gone"},
		{"从没跑过且未满窗口", logCollectNodeNone, false, spcRestoreIdlePolls - 1, false, ""},
		{"从没跑过且已满窗口", logCollectNodeNone, false, spcRestoreIdlePolls, true, "never_started"},
		{"运行中不恢复", logCollectNodeRunning, true, 0, false, ""},
		{"未知态不恢复", logCollectNodeUnknown, false, spcRestoreIdlePolls, false, ""},
	}
	for _, tc := range cases {
		due, reason := spcRestoreDue(tc.nodeState, tc.everRunning, tc.idlePolls)
		if due != tc.wantDue || reason != tc.wantReason {
			t.Fatalf("%s：得到 (%v,%q)，期望 (%v,%q)", tc.name, due, reason, tc.wantDue, tc.wantReason)
		}
	}
}

// TestSpcClassifyHosts 失败明细必须保留每台自己的原因码，不能再压成字符串。
func TestSpcClassifyHosts(t *testing.T) {
	results := []spcHostState{
		{IP: "10.0.0.1", Written: true},
		{IP: "10.0.0.2", Written: true},
		{IP: "10.0.0.3", ErrorCode: spcErrUnreachable},
		{IP: "10.0.0.4", ErrorCode: spcErrDirMissing},
		{IP: "10.0.0.5"},
	}
	written, failed := spcClassifyHosts(results)
	if written != 2 {
		t.Fatalf("written = %d", written)
	}
	if len(failed) != 3 {
		t.Fatalf("failed = %+v", failed)
	}
	// 按 IP 排序，且没有错误码的失败项保留空码（由 spcRejectionCode 兜底 write_failed）。
	for index, want := range []string{"10.0.0.3:host_unreachable", "10.0.0.4:spc_dir_missing", "10.0.0.5:"} {
		got := failed[index].IP + ":" + failed[index].ErrorCode
		if got != want {
			t.Fatalf("failed[%d] = %q，期望 %q", index, got, want)
		}
	}
}

// TestSpcRejectionCode 单一原因给精确码，多原因必须给聚合码。
// 用某一台的码代表全部，会让用户按错误的码排查。
func TestSpcRejectionCode(t *testing.T) {
	single := []spcHostState{{IP: "10.0.0.1", ErrorCode: "host_auth_failed"}, {IP: "10.0.0.2", ErrorCode: "host_auth_failed"}}
	errType, code := spcRejectionCode(single)
	if errType != "host_auth_failed" || code != ErrSpcSSHAuthFailed {
		t.Fatalf("单一原因得到 %s/%s", errType, code)
	}

	mixed := []spcHostState{{IP: "10.0.0.1", ErrorCode: "host_auth_failed"}, {IP: "10.0.0.2", ErrorCode: "spc_dir_missing"}}
	errType, code = spcRejectionCode(mixed)
	if errType != "spc_write_failed" || code != ErrSpcAggregate {
		t.Fatalf("混合原因应给聚合码，得到 %s/%s", errType, code)
	}
}

// TestSpcRejectionMessage 文案必须逐台给出「IP — 具体原因」，禁止再出现"节点不可达"这种泛化描述。
func TestSpcRejectionMessage(t *testing.T) {
	got := spcRejectionMessage(1, []spcHostState{
		{IP: "172.28.24.2", ErrorCode: "host_auth_failed"},
		{IP: "172.28.24.3", ErrorCode: "spc_dir_missing"},
	})
	// 1 台成功 + 2 台失败 = 目标 3 台；口径必须写成「目标 N 台，成功 M 台」，
	// 不能再写成「可用节点为 0」这种与集群可用节点数混淆的说法。
	for _, must := range []string{"目标 3 台", "成功 1 台", "172.28.24.2 — 节点 SSH 认证失败", "172.28.24.3 — 场景清单目录缺失"} {
		if !strings.Contains(got, must) {
			t.Fatalf("文案缺少 %q：%s", must, got)
		}
	}
	if strings.Contains(got, "节点不可达") {
		t.Fatalf("不应再出现泛化文案：%s", got)
	}
}

// TestClassifySpcCommandError 认证/超时/拒绝/文件缺失/读写失败必须分成不同的码。
//
// 重点防守：只读文件系统、权限不足这类「这一步没成功」的错误，绝不能
// 被归成「节点不可达」——那会把用户引去查网络，而真正要改的是文件系统与权限。
func TestClassifySpcCommandError(t *testing.T) {
	cases := []struct {
		name string
		err  error
		diag string
		step string
		want string
	}{
		{"认证失败", nil, "root@172.28.24.2: Permission denied (publickey).", spcStepDir, "host_auth_failed"},
		{"连接超时", nil, "ssh: connect to host 172.28.24.2 port 22: Connection timed out", spcStepDir, "host_timeout"},
		{"连接被拒", nil, "ssh: connect to host 172.28.24.2 port 22: Connection refused", spcStepDir, "host_unreachable"},
		{"文件缺失", nil, "cat: /sf/x/spc/core.json: No such file or directory", spcStepWrite, "spc_file_missing"},
		{"只读文件系统", nil, "cp: cannot create regular file: Read-only file system", spcStepWrite, "write_rejected"},
		{"空间不足", nil, "cp: error writing: No space left on device", spcStepBackup, "backup_failed"},
		{"权限不足", nil, "cp: cannot stat '/sf/x/spc/core.json': Permission denied", spcStepBackup, "backup_failed"},
		{"回滚失败", nil, "mv: cannot move: Operation not permitted", spcStepRestore, "spc_restore_failed"},
		{"无特征错误按步骤归因", nil, "exit status 1", spcStepWrite, "write_rejected"},
		{"ssh 客户端兜底", nil, "ssh: handshake failed: Connection closed by remote host", spcStepDir, "host_unreachable"},
	}
	for _, tc := range cases {
		got, detail := classifySpcCommandError(tc.err, tc.diag, tc.step)
		if got != tc.want {
			t.Fatalf("%s：得到 %s，期望 %s", tc.name, got, tc.want)
		}
		if detail == "" {
			t.Fatalf("%s：detail 不能为空，否则现场无法核对", tc.name)
		}
	}
}

// TestSpcJoinDiagKeepsStderr stderr 与 stdout 必须合并进诊断文本，
// 但目录解析只能用 stdout（"/bin/sh: ..." 会被误当成候选目录）。
func TestSpcJoinDiagKeepsStderr(t *testing.T) {
	if got := spcJoinDiag("SPC_WRITE_FAIL", "/bin/sh: cp: Read-only file system"); !strings.Contains(got, "Read-only") {
		t.Fatalf("stderr 必须进入诊断文本：%q", got)
	}
	if got := spcJoinDiag("", "only-stderr"); got != "only-stderr" {
		t.Fatalf("空 stdout 时应返回 stderr：%q", got)
	}
	if got := spcJoinDiag("only-stdout", ""); got != "only-stdout" {
		t.Fatalf("空 stderr 时应返回 stdout：%q", got)
	}
}

// TestSpcWrittenTargets 下发范围必须等于写入集合，多一台就是混合采集。
func TestSpcWrittenTargets(t *testing.T) {
	st := &spcTaskState{TaskID: "task1", Slot: logCollectSpcSlot}
	st.setHosts([]spcHostState{
		{IP: "10.0.0.9", Written: true},
		{IP: "10.0.0.1", Written: true},
		{IP: "10.0.0.5", ErrorCode: spcErrUnreachable},
	})
	if want := []string{"10.0.0.1", "10.0.0.9"}; !sameStrings(st.writtenTargets(), want) {
		t.Fatalf("writtenTargets = %v", st.writtenTargets())
	}
}

// TestSpcPersistHosts 落库字段名必须与平台契约（ip/backup/error）一致。
func TestSpcPersistHosts(t *testing.T) {
	persisted := spcPersistHosts([]spcHostState{{
		IP: "10.0.0.1", Dir: "/sf/x/spc", Backup: "/sf/x/spc/core.json.platform.task1.bak",
		Written: true, OriginalSHA: "abc", RestoreState: "success",
		ErrorCode: spcErrUnreachable, RestoreError: "",
	}})
	if len(persisted) != 1 {
		t.Fatalf("元素数 = %d", len(persisted))
	}
	item := persisted[0]
	if _, ok := item["error"]; !ok {
		t.Fatalf("失败原因必须写成 error 键：%v", item)
	}
	if _, ok := item["error_code"]; ok {
		t.Fatalf("落库不该出现内部键 error_code：%v", item)
	}
	for _, key := range []string{"ip", "dir", "backup", "original_sha256", "restore_state"} {
		if _, ok := item[key]; !ok {
			t.Fatalf("缺少字段 %q：%v", key, item)
		}
	}
}

// TestSpcTaskStateRestoreOnce 双路径（后台协程 + 平台 restore_spc）必须幂等。
func TestSpcTaskStateRestoreOnce(t *testing.T) {
	st := &spcTaskState{TaskID: "task1", Slot: logCollectSpcSlot}
	if !st.beginRestore() {
		t.Fatal("首次应取得恢复权")
	}
	if st.beginRestore() {
		t.Fatal("第二次必须被拒，否则会重复覆盖节点文件")
	}
	st.finishRestore("success", "task_completed", nil)
	if got := st.snapshot().RestoreState; got != "success" {
		t.Fatalf("还原态 = %q", got)
	}
}

// TestSpcTaskStateEverRunning 一旦观察过 running 就不能撤回：
// 否则"跑过之后消失"会被误判成"从未开始"，恢复窗口被无谓拉长。
func TestSpcTaskStateEverRunning(t *testing.T) {
	st := &spcTaskState{TaskID: "task1", Slot: logCollectSpcSlot}
	if st.hasRun() {
		t.Fatal("初始不应是 running")
	}
	st.noteState(logCollectNodeRunning)
	st.noteState(logCollectNodeNone)
	if !st.hasRun() {
		t.Fatal("running 一旦出现必须被记住")
	}
}

func TestSpcSnapshotCapsFailures(t *testing.T) {
	// progress 回包每 5s 落库一次，失败清单必须有上限，否则写放大。
	results := make([]spcHostState, 0, 12)
	for index := 0; index < 12; index++ {
		results = append(results, spcHostState{IP: "10.0.1." + string(rune('0'+index)), ErrorCode: spcErrWrite})
	}
	st := &spcTaskState{TaskID: "task1", Slot: logCollectSpcSlot}
	st.setHosts(results)
	snapshot := st.snapshot()
	if snapshot.HostCount != 12 || snapshot.Failed != 12 || snapshot.Written != 0 {
		t.Fatalf("计数不对：%+v", snapshot)
	}
	if len(snapshot.Failures) != 8 {
		t.Fatalf("失败明细应截断到 8 条，实际 %d", len(snapshot.Failures))
	}
}

func sameStrings(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for index := range got {
		if got[index] != want[index] {
			return false
		}
	}
	return true
}
