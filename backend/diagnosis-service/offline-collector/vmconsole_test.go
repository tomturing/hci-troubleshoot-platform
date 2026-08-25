package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// vtpshStubScript 是假 vtpsh：记录每次调用的完整 argv（逐 token），
// 并根据 --command 参数生成 screendump 文件（从 VTPSH_STUB_PPM 复制）。
const vtpshStubScript = `#!/bin/sh
{
    echo "INVOCATION"
    for arg in "$@"; do
        printf 'ARG\t%s\n' "$arg"
    done
} >> "$VTPSH_STUB_LOG"
command=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--command" ]; then
        command="$arg"
    fi
    prev="$arg"
done
case "$command" in
"screendump "*)
    target="${command#screendump }"
    cp "$VTPSH_STUB_PPM" "$target" || exit 3
    ;;
esac
exit 0
`

// setupVtpshStub 在 PATH 中放置假 vtpsh，并返回调用日志路径。
func setupVtpshStub(t *testing.T, ppm []byte) string {
	t.Helper()
	base := t.TempDir()
	binDir := filepath.Join(base, "bin")
	if err := os.MkdirAll(binDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(binDir, "vtpsh"), []byte(vtpshStubScript), 0o755); err != nil {
		t.Fatal(err)
	}
	ppmPath := filepath.Join(base, "source.ppm")
	if err := os.WriteFile(ppmPath, ppm, 0o600); err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(base, "vtpsh-calls.log")
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("VTPSH_STUB_LOG", logPath)
	t.Setenv("VTPSH_STUB_PPM", ppmPath)
	return logPath
}

// readStubInvocations 解析假 vtpsh 日志，返回每次调用的 argv token 列表。
func readStubInvocations(t *testing.T, logPath string) [][]string {
	t.Helper()
	content, err := os.ReadFile(logPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	invocations := make([][]string, 0)
	var current []string
	for _, line := range strings.Split(string(content), "\n") {
		if line == "INVOCATION" {
			if current != nil {
				invocations = append(invocations, current)
			}
			current = []string{}
		} else if strings.HasPrefix(line, "ARG\t") {
			current = append(current, strings.TrimPrefix(line, "ARG\t"))
		}
	}
	if current != nil {
		invocations = append(invocations, current)
	}
	return invocations
}

// vmConsoleTestItem 构造一个合法的 vm_console_capture 采集项。
func vmConsoleTestItem(itemID, hostNodeID, vmID string, maxCaptureBytes int64) collectorExecutionItem {
	return collectorExecutionItem{
		ItemID:           itemID,
		CollectorID:      "vm_console_kernel_panic",
		Executor:         executorVmConsoleCapture,
		TimeoutSeconds:   60,
		MaxOutputBytes:   16 * 1024 * 1024,
		OperationVersion: captureOperationVersionV1,
		CaptureMode:      captureModeBaselineThenPromptIfNearBlack,
		HostNodeID:       hostNodeID,
		VMID:             vmID,
		MaxCaptureBytes:  maxCaptureBytes,
		SourceSignalRefs: []sourceSignalRef{{SupportID: "KBD-TEST", SignalID: "s_vm_console_kernel_panic"}},
	}
}

// shrinkVmConsoleWindows 缩短固定窗口以便测试快速执行；返回恢复函数。
func shrinkVmConsoleWindows(t *testing.T) {
	t.Helper()
	oldSettle, oldPoll, oldPrompt := vmConsoleWakeSettleWindow, vmConsoleFilePollInterval, vmConsoleWakePromptTimeout
	vmConsoleWakeSettleWindow = time.Millisecond
	vmConsoleFilePollInterval = 5 * time.Millisecond
	vmConsoleWakePromptTimeout = 50 * time.Millisecond
	t.Cleanup(func() {
		vmConsoleWakeSettleWindow, vmConsoleFilePollInterval, vmConsoleWakePromptTimeout = oldSettle, oldPoll, oldPrompt
	})
}

// testNodeID 返回本机节点身份；若主机名不在白名单字符集内则跳过测试。
func testNodeID(t *testing.T) string {
	t.Helper()
	nodeID, err := os.Hostname()
	if err != nil || !hostNodeIDPattern.MatchString(nodeID) {
		t.Skipf("本机主机名 %q 不在节点标识白名单字符集内，跳过", nodeID)
	}
	return nodeID
}

func readCaptureResult(t *testing.T, outputDir, itemID string) vmConsoleCaptureResult {
	t.Helper()
	content, err := os.ReadFile(filepath.Join(outputDir, "captures", itemID, "capture-result.json"))
	if err != nil {
		t.Fatal(err)
	}
	var result vmConsoleCaptureResult
	if err := json.Unmarshal(content, &result); err != nil {
		t.Fatal(err)
	}
	return result
}

// contentPPM 是带内容的画面（亮暗相间条纹），不会命中近黑。
func contentPPM() []byte {
	return makePPM(64, 64, func(index int) (byte, byte, byte) {
		if (index/64)%2 == 0 {
			return 240, 240, 240
		}
		return 20, 20, 20
	})
}

// blackPPM 是纯黑画面，命中近黑。
func blackPPM() []byte {
	return solidPPM(64, 64, 0, 0, 0)
}

// ==== verify 阶段契约测试 ====

func TestVmConsoleVerifyAcceptsValidIntentAnd16MiBLimit(t *testing.T) {
	manifest := &artifactManifest{
		CollectionItems: []artifactItem{{PlanItemID: "item-vm", CollectorID: "vm_console_kernel_panic", Executor: executorVmConsoleCapture}},
	}
	artifact := &collectorArtifact{
		SchemaVersion:  "1.0",
		ExecutionItems: []collectorExecutionItem{vmConsoleTestItem("item-vm", "node-01", "123", 16777216)},
	}
	if err := validateCollectorArtifact(manifest, artifact); err != nil {
		t.Fatalf("合法 vm_console_capture 采集项应被接受：%v", err)
	}
}

func TestVmConsoleVerifyRejectsShellCharactersAtVerifyStage(t *testing.T) {
	manifest := &artifactManifest{
		CollectionItems: []artifactItem{{PlanItemID: "item-vm", CollectorID: "vm_console_kernel_panic", Executor: executorVmConsoleCapture}},
	}
	base := vmConsoleTestItem("item-vm", "node-01", "123", 16777216)
	cases := map[string]func(item *collectorExecutionItem){
		"host_node_id 含分号命令拼接":   func(item *collectorExecutionItem) { item.HostNodeID = "node;rm -rf /" },
		"host_node_id 含命令替换":     func(item *collectorExecutionItem) { item.HostNodeID = "$(reboot)" },
		"host_node_id 含反引号":      func(item *collectorExecutionItem) { item.HostNodeID = "node`id`" },
		"host_node_id 含路径穿越":     func(item *collectorExecutionItem) { item.HostNodeID = "../etc" },
		"host_node_id 为空":        func(item *collectorExecutionItem) { item.HostNodeID = "" },
		"vm_id 含 shell 字符":       func(item *collectorExecutionItem) { item.VMID = "12;3" },
		"vm_id 非数字":              func(item *collectorExecutionItem) { item.VMID = "vm-abc" },
		"vm_id 超长":               func(item *collectorExecutionItem) { item.VMID = "123456789012345678901" },
		"operation_version 非 v1": func(item *collectorExecutionItem) { item.OperationVersion = "v2" },
		"capture_mode 不受支持":      func(item *collectorExecutionItem) { item.CaptureMode = "free_form" },
		"timeout 超过 60 秒":        func(item *collectorExecutionItem) { item.TimeoutSeconds = 61 },
		"max_capture_bytes 超限":   func(item *collectorExecutionItem) { item.MaxCaptureBytes = 16*1024*1024 + 1 },
		"携带 argv 字段":             func(item *collectorExecutionItem) { item.Argv = []string{"/bin/echo"} },
	}
	for name, mutate := range cases {
		item := base
		mutate(&item)
		artifact := &collectorArtifact{SchemaVersion: "1.0", ExecutionItems: []collectorExecutionItem{item}}
		if err := validateCollectorArtifact(manifest, artifact); err == nil {
			t.Fatalf("%s：verify 阶段未拒绝非法采集项", name)
		}
		// parseCaptureIntent 同样必须拒绝（运行期防御层）；
		// argv 字段不属于采集意图结构，由 verify 分支单独拒绝。
		if _, err := parseCaptureIntent(item); err == nil && name != "携带 argv 字段" {
			t.Fatalf("%s：parseCaptureIntent 未拒绝非法意图", name)
		}
	}
}

func TestVmConsoleVerifyKeeps4MiBLimitForCommandExecutor(t *testing.T) {
	manifest := &artifactManifest{
		CollectionItems: []artifactItem{{PlanItemID: "item-1", CollectorID: "hostname", Executor: "command"}},
	}
	item := collectorExecutionItem{
		ItemID: "item-1", CollectorID: "hostname", Executor: "command",
		TimeoutSeconds: 30, MaxOutputBytes: 16 * 1024 * 1024, Argv: []string{"/bin/echo", "ok"},
	}
	artifact := &collectorArtifact{SchemaVersion: "1.0", ExecutionItems: []collectorExecutionItem{item}}
	if err := validateCollectorArtifact(manifest, artifact); err == nil {
		t.Fatal("command 执行器不得放宽到 16MiB，必须保持 4MiB 上限")
	}
	artifact.ExecutionItems[0].MaxOutputBytes = 4 * 1024 * 1024
	if err := validateCollectorArtifact(manifest, artifact); err != nil {
		t.Fatalf("command 执行器 4MiB 上限内应被接受：%v", err)
	}
}

// ==== 执行阶段测试（假 vtpsh 桩验证固定 argv） ====

func TestVmConsoleCaptureUsesFixedArgvAndProducesEvidence(t *testing.T) {
	shrinkVmConsoleWindows(t)
	nodeID := testNodeID(t)
	ppm := contentPPM()
	logPath := setupVtpshStub(t, ppm)
	outputDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}

	row := executeVmConsoleCaptureItem(vmConsoleTestItem("item-vm", nodeID, "123", 16777216), outputDir, true)
	if row.Status != "success" || row.ExitCode == nil || *row.ExitCode != 0 {
		t.Fatalf("合法采集项应成功：%+v", row)
	}

	// 假 vtpsh 收到的 argv 必须逐 token 精确等于固定命令。
	invocations := readStubInvocations(t, logPath)
	if len(invocations) != 1 {
		t.Fatalf("非近黑画面只应调用一次 vtpsh，实际 %d 次", len(invocations))
	}
	tokens := invocations[0]
	if len(tokens) != 4 {
		t.Fatalf("vtpsh argv 长度错误：%v", tokens)
	}
	expectedPath := "/nodes/" + nodeID + "/qemu/123/monitor"
	if tokens[0] != "create" || tokens[1] != expectedPath || tokens[2] != "--command" {
		t.Fatalf("vtpsh argv 前三个 token 错误：%v", tokens)
	}
	if !strings.HasPrefix(tokens[3], "screendump ") {
		t.Fatalf("Monitor 命令必须是 screendump：%v", tokens[3])
	}
	target := strings.TrimPrefix(tokens[3], "screendump ")
	if !strings.Contains(target, "hci-vm-console-") {
		t.Fatalf("截图必须落在采集器私有临时目录：%s", target)
	}
	uuidPattern := regexp.MustCompile(`/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.ppm$`)
	if !uuidPattern.MatchString(target) {
		t.Fatalf("截图文件名必须是运行时 UUID，不得使用 vm_id：%s", target)
	}
	if strings.Contains(filepath.Base(target), "123") {
		t.Fatalf("截图文件名不得使用 vm_id：%s", target)
	}

	// 证据文件与 capture-result.json 校验。
	baselinePath := filepath.Join(outputDir, "captures", "item-vm", "baseline.ppm")
	saved, err := os.ReadFile(baselinePath)
	if err != nil {
		t.Fatal(err)
	}
	if string(saved) != string(ppm) {
		t.Fatal("证据包中的 baseline.ppm 与截图原件不一致")
	}
	result := readCaptureResult(t, outputDir, "item-vm")
	if result.Executor != executorVmConsoleCapture || result.OperationVersion != captureOperationVersionV1 ||
		result.CaptureMode != captureModeBaselineThenPromptIfNearBlack ||
		result.HostNodeID != nodeID || result.VMID != "123" {
		t.Fatalf("capture-result.json 基础字段错误：%+v", result)
	}
	if result.WakeDecision != wakeDecisionNotNeeded || result.WakeResult != wakeResultNotAttempted {
		t.Fatalf("非近黑画面唤醒字段错误：%s/%s", result.WakeDecision, result.WakeResult)
	}
	if result.Recapture != nil {
		t.Fatal("非近黑画面不应有重截图")
	}
	expectedHash := sha256.Sum256(ppm)
	if result.Baseline.SHA256 != hex.EncodeToString(expectedHash[:]) || result.Baseline.SizeBytes != int64(len(ppm)) || result.Baseline.CapturedAt == "" {
		t.Fatalf("baseline 哈希/大小/时间错误：%+v", result.Baseline)
	}
	if result.Quality.ParseOK == false || result.Quality.NearBlack {
		t.Fatalf("带内容画面质量检查错误：%+v", result.Quality)
	}
	if result.Quality.AlgorithmRevision != "near-black-v1" {
		t.Fatalf("质量检查必须携带算法修订号：%+v", result.Quality)
	}
	if len(result.SourceSignalRefs) != 1 || result.SourceSignalRefs[0].SignalID != "s_vm_console_kernel_panic" {
		t.Fatalf("source_signal_refs 未保留：%+v", result.SourceSignalRefs)
	}
	if !result.Cleanup.Removed {
		t.Fatalf("临时文件应被清理：%+v", result.Cleanup)
	}
	for _, tempPath := range result.Cleanup.TempFiles {
		if _, err := os.Stat(tempPath); !os.IsNotExist(err) {
			t.Fatalf("宿主机临时文件未删除：%s", tempPath)
		}
	}
}

func TestVmConsoleNonInteractiveNeverSendsSendkey(t *testing.T) {
	shrinkVmConsoleWindows(t)
	nodeID := testNodeID(t)
	logPath := setupVtpshStub(t, blackPPM())
	outputDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}

	// 近黑画面 + 非交互模式：绝不发送 sendkey，保留基线图。
	row := executeVmConsoleCaptureItem(vmConsoleTestItem("item-vm", nodeID, "456", 16777216), outputDir, true)
	if row.Status != "success" || row.ExitCode == nil || *row.ExitCode != 0 {
		t.Fatalf("非交互模式基线截图应成功：%+v", row)
	}
	invocations := readStubInvocations(t, logPath)
	if len(invocations) != 1 {
		t.Fatalf("非交互模式只应调用一次 vtpsh（基线截图），实际 %d 次：%v", len(invocations), invocations)
	}
	for _, tokens := range invocations {
		for _, token := range tokens {
			if strings.Contains(token, "sendkey") {
				t.Fatalf("非交互模式绝不发送 sendkey：%v", tokens)
			}
		}
	}
	if _, err := os.Stat(filepath.Join(outputDir, "captures", "item-vm", "recapture-after-wake.ppm")); !os.IsNotExist(err) {
		t.Fatal("非交互模式不应生成重截图")
	}
	result := readCaptureResult(t, outputDir, "item-vm")
	if result.WakeDecision != wakeDecisionNonInteractive || result.WakeResult != wakeResultNotAttempted {
		t.Fatalf("唤醒字段错误：%s/%s", result.WakeDecision, result.WakeResult)
	}
	if !result.Quality.NearBlack {
		t.Fatalf("纯黑画面应命中近黑：%+v", result.Quality)
	}
}

func TestDefaultVmConsoleWakeConfirmWithoutTTYIsNonInteractive(t *testing.T) {
	// 与 hasInteractiveTTY 相同的探测：stdin 是字符设备且 /dev/tty 可打开时跳过。
	info, err := os.Stdin.Stat()
	if err == nil && info.Mode()&os.ModeCharDevice != 0 {
		if file, ttyErr := os.OpenFile("/dev/tty", os.O_RDWR, 0); ttyErr == nil {
			_ = file.Close()
			t.Skip("当前存在交互 TTY，无法验证非 TTY 分支")
		}
	}
	confirmed, decision := defaultVmConsoleWakeConfirm(false)
	if confirmed || decision != wakeDecisionNonInteractive {
		t.Fatalf("无交互 TTY 时必须返回 non_interactive：%v %s", confirmed, decision)
	}
}

func TestVmConsoleWakeConfirmedSendsFixedSendkeyAndRecaptures(t *testing.T) {
	shrinkVmConsoleWindows(t)
	nodeID := testNodeID(t)
	logPath := setupVtpshStub(t, blackPPM())
	outputDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	oldConfirm := vmConsoleWakeConfirm
	vmConsoleWakeConfirm = func(bool) (bool, string) { return true, wakeDecisionConfirmed }
	t.Cleanup(func() { vmConsoleWakeConfirm = oldConfirm })

	row := executeVmConsoleCaptureItem(vmConsoleTestItem("item-vm", nodeID, "789", 16777216), outputDir, false)
	if row.Status != "success" {
		t.Fatalf("确认唤醒后采集应成功：%+v", row)
	}
	invocations := readStubInvocations(t, logPath)
	if len(invocations) != 3 {
		t.Fatalf("确认唤醒应调用三次 vtpsh（基线/sendkey/重截），实际 %d 次", len(invocations))
	}
	// sendkey 的 argv 必须逐 token 精确等于固定命令。
	expectedPath := "/nodes/" + nodeID + "/qemu/789/monitor"
	sendkey := invocations[1]
	if len(sendkey) != 4 || sendkey[0] != "create" || sendkey[1] != expectedPath || sendkey[2] != "--command" || sendkey[3] != "sendkey down" {
		t.Fatalf("sendkey argv 不是固定命令：%v", sendkey)
	}
	if !strings.HasPrefix(invocations[2][3], "screendump ") {
		t.Fatalf("第三次调用必须是重截图：%v", invocations[2])
	}
	if _, err := os.Stat(filepath.Join(outputDir, "captures", "item-vm", "recapture-after-wake.ppm")); err != nil {
		t.Fatal("确认唤醒后应生成 recapture-after-wake.ppm")
	}
	result := readCaptureResult(t, outputDir, "item-vm")
	if result.WakeDecision != wakeDecisionConfirmed || result.WakeResult != wakeResultSuccess {
		t.Fatalf("唤醒字段错误：%s/%s", result.WakeDecision, result.WakeResult)
	}
	if result.Recapture == nil || result.Recapture.SHA256 == "" || result.Recapture.CapturedAt == "" {
		t.Fatalf("重截图信息缺失：%+v", result.Recapture)
	}
	if len(result.Cleanup.TempFiles) != 2 {
		t.Fatalf("应记录两个临时文件（基线+重截）：%+v", result.Cleanup)
	}
}

func TestVmConsoleWakeDeclinedAndTimedOutKeepBaseline(t *testing.T) {
	shrinkVmConsoleWindows(t)
	nodeID := testNodeID(t)
	cases := []struct {
		name     string
		decision string
	}{
		{name: "拒绝", decision: wakeDecisionDeclined},
		{name: "超时", decision: wakeDecisionTimedOut},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			logPath := setupVtpshStub(t, blackPPM())
			outputDir := t.TempDir()
			if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
				t.Fatal(err)
			}
			oldConfirm := vmConsoleWakeConfirm
			vmConsoleWakeConfirm = func(bool) (bool, string) { return false, test.decision }
			t.Cleanup(func() { vmConsoleWakeConfirm = oldConfirm })

			row := executeVmConsoleCaptureItem(vmConsoleTestItem("item-vm", nodeID, "321", 16777216), outputDir, false)
			if row.Status != "success" {
				t.Fatalf("拒绝/超时时基线图应保留且采集成功：%+v", row)
			}
			invocations := readStubInvocations(t, logPath)
			if len(invocations) != 1 {
				t.Fatalf("拒绝/超时不得发送 sendkey，vtpsh 调用次数：%d", len(invocations))
			}
			result := readCaptureResult(t, outputDir, "item-vm")
			if result.WakeDecision != test.decision || result.WakeResult != wakeResultNotAttempted {
				t.Fatalf("唤醒字段错误：%s/%s", result.WakeDecision, result.WakeResult)
			}
			if result.Recapture != nil {
				t.Fatal("拒绝/超时不应有重截图")
			}
		})
	}
}

func TestVmConsoleNodeIdentityMismatchRefusesExecution(t *testing.T) {
	shrinkVmConsoleWindows(t)
	logPath := setupVtpshStub(t, contentPPM())
	outputDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	// 签发目标指向其他节点：绝不执行 vtpsh。
	foreignNode := "other-node-01"
	if hostname, _ := os.Hostname(); hostname == foreignNode {
		foreignNode = "other-node-02"
	}
	row := executeVmConsoleCaptureItem(vmConsoleTestItem("item-vm", foreignNode, "123", 16777216), outputDir, true)
	if row.Status != "failed" || row.ExitCode == nil || *row.ExitCode == 0 {
		t.Fatalf("节点身份不一致必须失败：%+v", row)
	}
	invocations := readStubInvocations(t, logPath)
	if len(invocations) != 0 {
		t.Fatalf("节点身份不一致时绝不能调用 vtpsh：%v", invocations)
	}
	stderrContent, err := os.ReadFile(filepath.Join(outputDir, "commands", "item-vm.stderr"))
	if err != nil || !strings.HasPrefix(string(stderrContent), "vm_console_node_identity_mismatch:") {
		t.Fatalf("失败原因必须记录节点身份不一致：%q %v", stderrContent, err)
	}
	if reason := collectorFailureReason(outputDir, "item-vm", row.ExitCode); reason != "vm_console_node_identity_mismatch" {
		t.Fatalf("failure_reason 提取错误：%s", reason)
	}
}

func TestVmConsoleImageTooLargeFailClosed(t *testing.T) {
	shrinkVmConsoleWindows(t)
	nodeID := testNodeID(t)
	// 桩输出约 12KB（64x64x3+头），max_capture_bytes 限制为 1KB → 超限拒绝。
	setupVtpshStub(t, contentPPM())
	outputDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	row := executeVmConsoleCaptureItem(vmConsoleTestItem("item-vm", nodeID, "123", 1024), outputDir, true)
	if row.Status != "failed" {
		t.Fatalf("超过 max_capture_bytes 必须失败：%+v", row)
	}
	if reason := collectorFailureReason(outputDir, "item-vm", row.ExitCode); reason != "vm_console_image_invalid" {
		t.Fatalf("failure_reason 应为 vm_console_image_invalid：%s", reason)
	}
}

// TestVmConsoleNoAutoApproveWakeFlag 守护"无任何参数能预先自动同意唤醒"：
// 采集器不存在唤醒自动同意类命令行参数（未知参数一律报错）。
func TestVmConsoleNoAutoApproveWakeFlag(t *testing.T) {
	for _, flag := range []string{"--auto-approve-wake", "--yes-wake", "--assume-yes-wake"} {
		if _, err := parseOptions([]string{"--expected-root-fingerprint", strings.Repeat("a", 64), flag}); err == nil {
			t.Fatalf("不得存在唤醒自动同意参数：%s", flag)
		}
	}
}

// ==== 打包阶段测试（manifest 扩展） ====

func TestVmConsoleManifestDeclaresCaptureFilesAndAuditFields(t *testing.T) {
	outputDir := t.TempDir()
	captureDir := filepath.Join(outputDir, "captures", "item-vm")
	if err := os.MkdirAll(captureDir, 0o700); err != nil {
		t.Fatal(err)
	}
	ppm := blackPPM()
	if err := os.WriteFile(filepath.Join(captureDir, "baseline.ppm"), ppm, 0o600); err != nil {
		t.Fatal(err)
	}
	captureResult := vmConsoleCaptureResult{
		Executor:         executorVmConsoleCapture,
		OperationVersion: captureOperationVersionV1,
		CaptureMode:      captureModeBaselineThenPromptIfNearBlack,
		HostNodeID:       "node-01",
		VMID:             "123",
		Baseline:         vmConsoleCaptureFileInfo{CapturedAt: "2026-08-20T00:00:00Z", ExitCode: 0, SHA256: strings.Repeat("0", 64), SizeBytes: int64(len(ppm))},
		Quality: vmConsoleQualityResult{
			AlgorithmRevision: nearBlackAlgorithmRevision, ParseOK: true, NearBlack: true,
			Metrics: map[string]any{"mean_luma": 0.0, "non_black_ratio": 0.0, "edge_density": 0.0},
		},
		WakeDecision: wakeDecisionNonInteractive,
		WakeResult:   wakeResultNotAttempted,
		Cleanup:      vmConsoleCleanupResult{Removed: true},
	}
	resultBytes, err := json.Marshal(captureResult)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(captureDir, "capture-result.json"), resultBytes, 0o600); err != nil {
		t.Fatal(err)
	}

	artifact := &artifactManifest{
		SessionID: "session-1", CollectionPlanID: "plan-1",
		BundleEncryption: &bundleEncryption{Algorithm: "AES-256-GCM", KeyWrapAlgorithm: "RSA-OAEP-SHA256", KeyID: "rsa-test", Format: "HCIEB2"},
		CollectionItems: []artifactItem{{
			PlanItemID: "item-vm", CollectorID: "vm_console_kernel_panic", Executor: executorVmConsoleCapture,
			TimeWindow: timeWindow{StartTime: "2026-08-20T00:00:00Z", EndTime: "2026-08-20T01:00:00Z"},
		}},
	}
	zero := 0
	manifest, err := buildEvidenceManifest(
		packageOptions{outputDir: outputDir, source: "node-01", timezone: "Asia/Shanghai", bundleType: "initial"},
		artifact,
		&caseDocument{CaseID: "Q-VM-TEST", SessionID: "session-1"},
		[]executionRow{{ItemID: "item-vm", CollectorID: "vm_console_kernel_panic", ExitCode: &zero, Status: "success"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(manifest.CollectionItems) != 1 {
		t.Fatalf("采集项数量错误：%d", len(manifest.CollectionItems))
	}
	item := manifest.CollectionItems[0]
	if item.Status != "success" {
		t.Fatalf("采集项状态错误：%s", item.Status)
	}
	// 逐项声明：path/media_type/sensitivity/size_bytes/sha256。
	want := map[string][2]string{
		"captures/item-vm/baseline.ppm":        {"image/x-portable-pixmap", "confidential"},
		"captures/item-vm/capture-result.json": {"application/json", "confidential"},
	}
	if len(item.Files) != len(want) {
		t.Fatalf("证据文件数量错误：%+v", item.Files)
	}
	for _, file := range item.Files {
		expected, ok := want[file.Path]
		if !ok {
			t.Fatalf("意外的证据文件：%s", file.Path)
		}
		if file.MediaType != expected[0] || file.Sensitivity != expected[1] {
			t.Fatalf("证据文件 media_type/sensitivity 错误：%+v", file)
		}
		if file.SHA256 == "" || file.SizeBytes <= 0 {
			t.Fatalf("证据文件缺少哈希或大小：%+v", file)
		}
	}
	// collection_items[] 附加审计字段。
	if item.VmConsole == nil {
		t.Fatal("manifest 缺少 vm_console 附加字段")
	}
	details := item.VmConsole
	if details.Executor != executorVmConsoleCapture || details.OperationVersion != captureOperationVersionV1 ||
		details.HostNodeID != "node-01" || details.VMID != "123" ||
		details.WakeDecision != wakeDecisionNonInteractive || details.WakeResult != wakeResultNotAttempted ||
		!details.NearBlack || details.NearBlackAlgorithm != "near-black-v1" || details.RecaptureGenerated {
		t.Fatalf("vm_console 附加字段错误：%+v", details)
	}
	if details.QualityMetrics["mean_luma"] != 0.0 {
		t.Fatalf("近黑指标未入库：%+v", details.QualityMetrics)
	}
}

func TestVmConsoleManifestFailedItemRecordsFailureReason(t *testing.T) {
	outputDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDir, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outputDir, "commands", "item-vm.stderr"),
		[]byte("vm_console_node_identity_mismatch: 本机节点身份不一致\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	artifact := &artifactManifest{
		SessionID: "session-1", CollectionPlanID: "plan-1",
		BundleEncryption: &bundleEncryption{Algorithm: "AES-256-GCM", KeyWrapAlgorithm: "RSA-OAEP-SHA256", KeyID: "rsa-test", Format: "HCIEB2"},
		CollectionItems: []artifactItem{{
			PlanItemID: "item-vm", CollectorID: "vm_console_kernel_panic", Executor: executorVmConsoleCapture,
			TimeWindow: timeWindow{StartTime: "2026-08-20T00:00:00Z", EndTime: "2026-08-20T01:00:00Z"},
		}},
	}
	exitCode := 126
	manifest, err := buildEvidenceManifest(
		packageOptions{outputDir: outputDir, source: "node-01", timezone: "Asia/Shanghai", bundleType: "initial"},
		artifact,
		&caseDocument{CaseID: "Q-VM-TEST", SessionID: "session-1"},
		[]executionRow{{ItemID: "item-vm", CollectorID: "vm_console_kernel_panic", ExitCode: &exitCode, Status: "failed"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	item := manifest.CollectionItems[0]
	if item.Status != "failed" || item.FailureReason == nil || *item.FailureReason != "vm_console_node_identity_mismatch" {
		t.Fatalf("失败原因未记入 collection_items[].failure_reason：%+v", item)
	}
}

// TestVmConsoleEndToEndViaRunCollection 通过 runCollection 分发验证执行器接入。
func TestVmConsoleEndToEndViaRunCollection(t *testing.T) {
	shrinkVmConsoleWindows(t)
	nodeID := testNodeID(t)
	setupVtpshStub(t, contentPPM())
	outputDir := t.TempDir()
	artifact := &collectorArtifact{
		ExecutionItems: []collectorExecutionItem{
			vmConsoleTestItem("item-vm", nodeID, "123", 16777216),
			// 既有 command 执行器行为不受影响。
			{ItemID: "item-cmd", CollectorID: "hostname", Executor: "command", TimeoutSeconds: 10, MaxOutputBytes: 1024, Argv: []string{"/bin/echo", "ok"}},
		},
	}
	if err := runCollection(artifact, outputDir, true); err != nil {
		t.Fatal(err)
	}
	rows, stats, err := loadExecutionRows(outputDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || stats.Success != 2 || stats.Failed != 0 {
		t.Fatalf("执行结果错误：%+v %+v", rows, stats)
	}
}
