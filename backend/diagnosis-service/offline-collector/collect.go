package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type outputBudget struct {
	mutex     sync.Mutex
	remaining int64
	truncated bool
}

type cappedWriter struct {
	file     *os.File
	budget   *outputBudget
	retained int64
}

func (writer *cappedWriter) Write(content []byte) (int, error) {
	writer.budget.mutex.Lock()
	defer writer.budget.mutex.Unlock()
	allowed := int64(len(content))
	if allowed > writer.budget.remaining {
		allowed = writer.budget.remaining
		writer.budget.truncated = true
	}
	if allowed > 0 {
		written, err := writer.file.Write(content[:allowed])
		writer.retained += int64(written)
		writer.budget.remaining -= int64(written)
		if err != nil {
			return 0, err
		}
	}
	return len(content), nil
}

func formatTarget(raw json.RawMessage) string {
	var target map[string]any
	if json.Unmarshal(raw, &target) == nil {
		return fmt.Sprintf("%v/%v", target["type"], target["id"])
	}
	return string(raw)
}

func renderScopeSummary(artifact *artifactManifest, caseData *caseDocument) {
	counts := map[string]int{"command": 0, "http": 0, "manual": 0, executorVmConsoleCapture: 0}
	for _, item := range artifact.CollectionItems {
		if _, ok := counts[item.Executor]; ok {
			counts[item.Executor]++
		} else {
			counts["manual"]++
		}
	}
	fmt.Println("[2/4] 采集范围摘要")
	fmt.Printf("  工单：%s    场景：%s\n", caseData.CaseID, caseData.SelectedScenario)
	fmt.Printf("  故障窗口：%s ~ %s\n", caseData.IncidentWindow["start"], caseData.IncidentWindow["end"])
	fmt.Printf("  制品：artifact_id=%s  target_key=%s  过期时间：%s\n", artifact.ArtifactID, artifact.TargetKey, artifact.Signature.ExpiresAt)
	fmt.Printf("  采集项（共 %d 项：直接命令 %d / HCI API %d / 虚拟机控制台截图 %d / 人工附件 %d）：\n", len(artifact.CollectionItems), counts["command"], counts["http"], counts[executorVmConsoleCapture], counts["manual"])
	for index, item := range artifact.CollectionItems {
		fmt.Printf("  [%3d] collector_id=%s  executor=%s  target=%s  窗口=%s~%s  输出=%s\n",
			index+1, item.CollectorID, item.Executor, formatTarget(item.Target), item.TimeWindow.StartTime,
			item.TimeWindow.EndTime, item.OutputContract.OutputPath)
	}
	if counts["http"] > 0 && (os.Getenv("HCI_API_BASE_URL") == "" || os.Getenv("HCI_API_TOKEN") == "") {
		fmt.Println("  警告：清单包含 HCI API Collector，但未设置 HCI_API_BASE_URL / HCI_API_TOKEN；对应项将采集失败。")
	}
	if counts["manual"] > 0 {
		fmt.Println("  提示：人工附件项会生成 manual-guides/*.txt 指引，请按指引放置附件。")
	}
	if counts[executorVmConsoleCapture] > 0 {
		fmt.Println("  提示：虚拟机控制台截图项仅执行固定 screendump；若首张截图近黑，将在本机 TTY 请求人工确认后才发送一次向下方向键唤醒。")
	}
}

func confirmScope(skip bool) error {
	if skip {
		fmt.Println("  已通过 --yes 跳过人工确认（自动化模式）。")
		return nil
	}
	info, err := os.Stdin.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return &runnerError{code: exitDeclined, message: "非交互环境必须显式传入 --yes 才会执行采集；已中止"}
	}
	fmt.Print("确认在本机执行以上采集范围？[y/N]: ")
	reader := bufio.NewReader(os.Stdin)
	answer, _ := reader.ReadString('\n')
	answer = strings.ToLower(strings.TrimSpace(answer))
	if answer != "y" && answer != "yes" {
		return &runnerError{code: exitDeclined, message: "用户拒绝执行采集范围；已中止"}
	}
	return nil
}

func runCollection(artifact *collectorArtifact, outputDir string, nonInteractive bool) error {
	fmt.Println("[3/4] 执行结构化采集制品 …")
	for _, relative := range []string{"commands", "manual-guides", "attachments", "captures"} {
		if err := os.MkdirAll(filepath.Join(outputDir, relative), 0o700); err != nil {
			return fmt.Errorf("无法创建采集输出目录：%w", err)
		}
	}
	manifestPath := filepath.Join(outputDir, "execution-manifest.jsonl")
	manifestFile, err := os.OpenFile(manifestPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("无法创建执行清单：%w", err)
	}
	defer manifestFile.Close()
	encoder := json.NewEncoder(manifestFile)
	for _, item := range artifact.ExecutionItems {
		var row executionRow
		switch item.Executor {
		case "command":
			row = executeCommandItem(item, outputDir)
		case "http":
			row = executeHTTPItem(item, outputDir)
		case "manual":
			row, err = prepareManualItem(item, outputDir)
			if err != nil {
				return err
			}
		case executorVmConsoleCapture:
			row = executeVmConsoleCaptureItem(item, outputDir, nonInteractive)
		default:
			return fmt.Errorf("不支持的结构化执行器：%s", item.Executor)
		}
		if err := encoder.Encode(row); err != nil {
			return fmt.Errorf("无法写入执行清单：%w", err)
		}
	}
	if err := manifestFile.Sync(); err != nil {
		return fmt.Errorf("无法持久化执行清单：%w", err)
	}
	fmt.Printf("  采集完成，输出目录：%s\n", outputDir)
	return nil
}

func openItemOutputs(item collectorExecutionItem, outputDir string) (*os.File, *os.File, *cappedWriter, *cappedWriter, *outputBudget, error) {
	stdoutFile, err := os.OpenFile(filepath.Join(outputDir, "commands", item.ItemID+".stdout"), os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	stderrFile, err := os.OpenFile(filepath.Join(outputDir, "commands", item.ItemID+".stderr"), os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		stdoutFile.Close()
		return nil, nil, nil, nil, nil, err
	}
	budget := &outputBudget{remaining: item.MaxOutputBytes}
	return stdoutFile, stderrFile, &cappedWriter{file: stdoutFile, budget: budget}, &cappedWriter{file: stderrFile, budget: budget}, budget, nil
}

func executeCommandItem(item collectorExecutionItem, outputDir string) executionRow {
	stdoutFile, stderrFile, stdoutWriter, stderrWriter, budget, err := openItemOutputs(item, outputDir)
	exitCode := 127
	if err != nil {
		return executionRow{ItemID: item.ItemID, CollectorID: item.CollectorID, ExitCode: &exitCode, Status: "failed"}
	}
	defer stdoutFile.Close()
	defer stderrFile.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(item.TimeoutSeconds)*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, item.Argv[0], item.Argv[1:]...)
	command.Stdin = nil
	command.Stdout = stdoutWriter
	command.Stderr = stderrWriter
	runErr := command.Run()
	if ctx.Err() == context.DeadlineExceeded {
		exitCode = 124
		_, _ = stderrWriter.Write([]byte("采集命令执行超时\n"))
	} else if runErr == nil {
		exitCode = 0
	} else if exitError, ok := runErr.(*exec.ExitError); ok {
		exitCode = exitError.ExitCode()
	} else {
		_, _ = stderrWriter.Write([]byte(fmt.Sprintf("无法启动采集命令：%v\n", runErr)))
	}
	status := "failed"
	if exitCode == 0 {
		status = "success"
	}
	return executionRow{
		ItemID: item.ItemID, CollectorID: item.CollectorID, ExitCode: &exitCode, Status: status,
		StdoutBytes: stdoutWriter.retained, StderrBytes: stderrWriter.retained, OutputTruncated: budget.truncated,
	}
}

func executeHTTPItem(item collectorExecutionItem, outputDir string) executionRow {
	stdoutFile, stderrFile, stdoutWriter, stderrWriter, budget, err := openItemOutputs(item, outputDir)
	exitCode := 126
	if err != nil {
		return executionRow{ItemID: item.ItemID, CollectorID: item.CollectorID, ExitCode: &exitCode, Status: "failed"}
	}
	defer stdoutFile.Close()
	defer stderrFile.Close()
	baseValue := os.Getenv("HCI_API_BASE_URL")
	token := os.Getenv("HCI_API_TOKEN")
	if baseValue == "" || token == "" {
		_, _ = stderrWriter.Write([]byte("必须设置 HCI_API_BASE_URL 和执行时短期 HCI_API_TOKEN\n"))
	} else if target, targetErr := resolveAPIURL(baseValue, item.Path); targetErr != nil {
		exitCode = 2
		_, _ = stderrWriter.Write([]byte(targetErr.Error() + "\n"))
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(item.TimeoutSeconds)*time.Second)
		request, requestErr := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if requestErr != nil {
			exitCode = 2
			_, _ = stderrWriter.Write([]byte(requestErr.Error() + "\n"))
		} else {
			request.Header.Set("Authorization", "Bearer "+token)
			client := &http.Client{
				Transport:     &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}},
				CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
			}
			response, requestErr := client.Do(request)
			if requestErr != nil {
				if ctx.Err() == context.DeadlineExceeded {
					exitCode = 124
				} else {
					exitCode = 7
				}
				_, _ = stderrWriter.Write([]byte(requestErr.Error() + "\n"))
			} else {
				_, copyErr := io.Copy(stdoutWriter, response.Body)
				response.Body.Close()
				if copyErr != nil {
					exitCode = 23
					_, _ = stderrWriter.Write([]byte(copyErr.Error() + "\n"))
				} else if response.StatusCode < 200 || response.StatusCode >= 300 {
					exitCode = 22
					_, _ = stderrWriter.Write([]byte(fmt.Sprintf("HCI API 返回 HTTP %d\n", response.StatusCode)))
				} else {
					exitCode = 0
				}
			}
		}
		cancel()
	}
	status := "failed"
	if exitCode == 0 {
		status = "success"
	}
	return executionRow{
		ItemID: item.ItemID, CollectorID: item.CollectorID, ExitCode: &exitCode, Status: status,
		StdoutBytes: stdoutWriter.retained, StderrBytes: stderrWriter.retained, OutputTruncated: budget.truncated,
	}
}

func resolveAPIURL(baseValue, relativeValue string) (string, error) {
	baseURL, err := url.Parse(baseValue)
	if err != nil || baseURL.Scheme != "https" || baseURL.Host == "" || baseURL.User != nil {
		return "", fmt.Errorf("HCI_API_BASE_URL 必须是无用户信息的 HTTPS 地址")
	}
	relativeURL, err := url.Parse(relativeValue)
	if err != nil || relativeURL.IsAbs() || relativeURL.Host != "" || !strings.HasPrefix(relativeURL.Path, "/") {
		return "", fmt.Errorf("HCI API 路径不合法")
	}
	decodedPath, err := url.PathUnescape(relativeURL.EscapedPath())
	if err != nil || strings.Contains(decodedPath, "..") {
		return "", fmt.Errorf("HCI API 路径禁止父级跳转")
	}
	target := baseURL.ResolveReference(relativeURL)
	if target.Scheme != baseURL.Scheme || target.Host != baseURL.Host {
		return "", fmt.Errorf("HCI API 地址越过允许的基础地址")
	}
	return target.String(), nil
}

func prepareManualItem(item collectorExecutionItem, outputDir string) (executionRow, error) {
	path := filepath.Join(outputDir, "manual-guides", item.ItemID+".txt")
	if err := os.WriteFile(path, []byte(item.Guide+"\n"), 0o600); err != nil {
		return executionRow{}, fmt.Errorf("无法写入人工附件指引：%w", err)
	}
	return executionRow{ItemID: item.ItemID, CollectorID: item.CollectorID, Status: "awaiting_manual_attachment"}, nil
}

// ==== vm_console_capture 专用执行器（qkv_vm_console 离线路径） ====
//
// 安全契约（设计文档 §3.4 / §9.3，硬性要求）：
//  1. 只接受签名制品内冻结的结构化采集意图（captureIntent），任一校验失败即拒绝执行；
//  2. 本机节点身份必须与签发目标一致，不符绝不执行 vtpsh；
//  3. 以固定参数数组调用 vtpsh（exec.Command 直接执行），不启动 Shell、不解析 Shell、
//     不支持环境变量展开、命令拼接、重定向或任意 Monitor 指令；
//  4. 唯一允许的 Monitor 操作是 screendump 与经人工确认的一次 sendkey down；
//  5. 截图文件不超过 max_capture_bytes，且仅落在采集器私有临时目录；
//  6. 任何检查失败均记入 collection_items[].failure_reason，继续执行其他采集项；
//  7. 不存在任何命令行参数或环境变量能把唤醒预先设为"自动同意"。

const (
	// vmConsoleExecutable 为固定的 vtpsh 可执行名（PATH 查找）；平台确认后如需绝对路径只改此常量。
	vmConsoleExecutable = "vtpsh"
	// 唤醒确认后等待画面稳定的固定短窗口（秒），不得循环重试。
	vmConsoleWakeSettleSecondsDefault = 5
	// TTY 唤醒确认输入超时（秒）；超时按拒绝处理。
	vmConsoleWakePromptTimeoutSecondsDefault = 120
	// vtpsh 诊断输出（stdout/stderr）缓冲上限，防止异常输出耗尽内存。
	vmConsoleDiagnosticBufferBytes = 64 * 1024
)

var (
	// 以下时长变量保持设计文档规定的默认值；仅测试可按需缩短，生产路径不读取其他配置。
	vmConsoleWakeSettleWindow  = vmConsoleWakeSettleSecondsDefault * time.Second
	vmConsoleWakePromptTimeout = vmConsoleWakePromptTimeoutSecondsDefault * time.Second
	vmConsoleFilePollInterval  = 250 * time.Millisecond
	// vmConsoleWakeConfirm 询问操作员是否唤醒并返回 (是否确认, 唤醒决定)。
	// 生产实现为 defaultVmConsoleWakeConfirm（TTY 检测 + 人工输入）；测试可替换。
	vmConsoleWakeConfirm = defaultVmConsoleWakeConfirm
)

// boundedBuffer 是有界内存缓冲，实现 io.Writer；超出上限的内容直接丢弃。
type boundedBuffer struct {
	data  []byte
	limit int
}

func (buffer *boundedBuffer) Write(content []byte) (int, error) {
	if remaining := buffer.limit - len(buffer.data); remaining > 0 {
		if len(content) > remaining {
			buffer.data = append(buffer.data, content[:remaining]...)
		} else {
			buffer.data = append(buffer.data, content...)
		}
	}
	return len(content), nil
}

func (buffer *boundedBuffer) String() string { return string(buffer.data) }

// vtpshMonitorPath 拼装签名目标的 QEMU Monitor 访问路径。
// host_node_id 与 vm_id 均已通过 verify 阶段的白名单字符集校验，此处不拼接任何用户文本。
func vtpshMonitorPath(intent captureIntent) string {
	return "/nodes/" + intent.HostNodeID + "/qemu/" + intent.VMID + "/monitor"
}

// runVtpshMonitorCommand 以固定参数数组调用 vtpsh 执行一次 Monitor 操作。
// monitorCommand 只能是代码常量（"screendump <私有临时路径>" 或 "sendkey down"），
// 绝不接受外部文本；命令不经 Shell，超时由独立 context 控制。
func runVtpshMonitorCommand(intent captureIntent, monitorCommand string, timeoutSeconds int) (int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSeconds)*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, vmConsoleExecutable, "create", vtpshMonitorPath(intent), "--command", monitorCommand)
	command.Stdin = nil
	stdout := &boundedBuffer{limit: vmConsoleDiagnosticBufferBytes}
	stderr := &boundedBuffer{limit: vmConsoleDiagnosticBufferBytes}
	command.Stdout = stdout
	command.Stderr = stderr
	runErr := command.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return 124, fmt.Errorf("vtpsh 执行超时（%d 秒上限）", timeoutSeconds)
	}
	if runErr == nil {
		return 0, nil
	}
	if exitError, ok := runErr.(*exec.ExitError); ok {
		message := strings.TrimSpace(stderr.String())
		if message != "" {
			return exitError.ExitCode(), fmt.Errorf("vtpsh 退出码 %d：%s", exitError.ExitCode(), message)
		}
		return exitError.ExitCode(), fmt.Errorf("vtpsh 退出码 %d", exitError.ExitCode())
	}
	return 127, fmt.Errorf("无法启动 vtpsh：%v", runErr)
}

// runVtpshScreendump 执行固定的基线/重截图操作：screendump <私有临时路径>。
func runVtpshScreendump(intent captureIntent, targetPath string, timeoutSeconds int) (int, error) {
	return runVtpshMonitorCommand(intent, "screendump "+targetPath, timeoutSeconds)
}

// waitForFile 有界轮询等待 screendump 文件出现（非空普通文件）；超出截止时间立即返回，绝不无限等待。
func waitForFile(path string, deadline time.Time) error {
	for {
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() && info.Size() > 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("截图文件 %s 未在时限内出现", filepath.Base(path))
		}
		time.Sleep(vmConsoleFilePollInterval)
	}
}

// readCaptureFile 按大小上限读取截图文件；超限或读取失败均 fail-closed。
func readCaptureFile(path string, maxBytes int64) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("无法读取截图文件：%w", err)
	}
	if info.Size() > maxBytes {
		return nil, fmt.Errorf("截图文件 %d 字节超过上限 %d 字节", info.Size(), maxBytes)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("无法读取截图文件：%w", err)
	}
	if int64(len(raw)) > maxBytes {
		return nil, fmt.Errorf("截图文件 %d 字节超过上限 %d 字节", len(raw), maxBytes)
	}
	return raw, nil
}

// localNodeIdentity 返回本机节点身份。采集器现有身份来源是主机名
// （--source 缺省值同样取自主机名），此处只信任主机名，不接受任何外部覆盖。
func localNodeIdentity() string {
	nodeID, _ := os.Hostname()
	return nodeID
}

// executeVmConsoleCaptureItem 执行一个 vm_console_capture 采集项。
// 基线截图成功即记成功（exit 0）；唤醒是否执行不影响采集项成败，
// 唤醒失败时保留基线图并在 capture-result.json 中记录 wake_result。
func executeVmConsoleCaptureItem(item collectorExecutionItem, outputDir string, nonInteractive bool) executionRow {
	// fail 沿用现有失败记录方式：写 commands/<item>.stderr（打包阶段由
	// collectorFailureReason 提取 "vm_console_<原因>" 前缀写入 failure_reason），
	// 并返回 failed 执行行；其他采集项继续执行。
	fail := func(exitCode int, reason string) executionRow {
		stderrPath := filepath.Join(outputDir, "commands", item.ItemID+".stderr")
		_ = os.WriteFile(stderrPath, []byte(reason+"\n"), 0o600)
		fmt.Printf("  - 虚拟机控制台采集项 %s 失败：%s\n", item.ItemID, reason)
		code := exitCode
		return executionRow{ItemID: item.ItemID, CollectorID: item.CollectorID, ExitCode: &code, Status: "failed"}
	}

	// 1. 防御性复核采集意图（verify 阶段已校验；运行期绝不执行非法意图）。
	intent, err := parseCaptureIntent(item)
	if err != nil {
		return fail(2, "vm_console_intent_invalid: "+err.Error())
	}
	// 2. 本机节点身份校验：不符立即拒绝，绝不执行 vtpsh。
	nodeID := localNodeIdentity()
	if nodeID == "" || nodeID != intent.HostNodeID {
		return fail(126, fmt.Sprintf("vm_console_node_identity_mismatch: 本机节点身份 %q 与签发目标 %q 不一致，拒绝执行截图", nodeID, intent.HostNodeID))
	}
	// 3. 私有临时目录（0700）；截图仅落在该目录内。
	tempDir, err := os.MkdirTemp("", "hci-vm-console-")
	if err != nil {
		return fail(1, "vm_console_tempdir_failed: "+err.Error())
	}
	_ = os.Chmod(tempDir, 0o700)

	tempFiles := make([]string, 0, 2)
	cleanupTemp := func() vmConsoleCleanupResult {
		result := vmConsoleCleanupResult{TempDir: tempDir, TempFiles: tempFiles, Removed: true}
		if removeErr := os.RemoveAll(tempDir); removeErr != nil {
			// 清理失败只记录不阻断。
			result.Removed = false
			result.Error = removeErr.Error()
			fmt.Printf("  - 警告：虚拟机控制台截图临时文件清理失败：%v\n", removeErr)
		}
		return result
	}

	// 4. 基线截图：capture-id 为运行时 UUID（不用 vm_id 做文件名）。
	captureID, err := newUUID()
	if err != nil {
		_ = os.RemoveAll(tempDir)
		return fail(1, "vm_console_capture_id_failed: "+err.Error())
	}
	baselineTempPath := filepath.Join(tempDir, captureID+".ppm")
	tempFiles = append(tempFiles, baselineTempPath)
	baselineDeadline := time.Now().Add(time.Duration(intent.TimeoutSeconds) * time.Second)
	baselineExit, baselineErr := runVtpshScreendump(intent, baselineTempPath, intent.TimeoutSeconds)
	if baselineErr != nil {
		_ = os.RemoveAll(tempDir)
		code := baselineExit
		if code == 0 {
			code = 1
		}
		return fail(code, fmt.Sprintf("vm_console_baseline_capture_failed: %v", baselineErr))
	}
	if err := waitForFile(baselineTempPath, baselineDeadline); err != nil {
		_ = os.RemoveAll(tempDir)
		return fail(124, "vm_console_screendump_timeout: "+err.Error())
	}
	baselineRaw, err := readCaptureFile(baselineTempPath, intent.MaxCaptureBytes)
	if err != nil {
		_ = os.RemoveAll(tempDir)
		return fail(126, "vm_console_image_invalid: "+err.Error())
	}
	baselineCapturedAt := time.Now().UTC().Format(time.RFC3339Nano)
	baselineHash := sha256.Sum256(baselineRaw)

	// 5. 确定性近黑质量检查（与平台侧 Python 实现同算法修订）。
	quality := analyzePPMNearBlack(baselineRaw)

	// 6. 写入证据输出目录 captures/<collection-item-id>/。
	captureDir := filepath.Join(outputDir, "captures", item.ItemID)
	if err := os.MkdirAll(captureDir, 0o700); err != nil {
		_ = os.RemoveAll(tempDir)
		return fail(1, "vm_console_output_dir_failed: "+err.Error())
	}
	if err := os.WriteFile(filepath.Join(captureDir, "baseline.ppm"), baselineRaw, 0o600); err != nil {
		_ = os.RemoveAll(tempDir)
		return fail(1, "vm_console_output_write_failed: "+err.Error())
	}

	// 7. 唤醒决定流程：仅在近黑命中时申请；非交互/无 TTY/拒绝/超时一律不发键。
	wakeDecision := wakeDecisionNotNeeded
	wakeResult := wakeResultNotAttempted
	var recaptureInfo *vmConsoleCaptureFileInfo
	if quality.NearBlack {
		confirmed, decision := vmConsoleWakeConfirm(nonInteractive)
		wakeDecision = decision
		if confirmed {
			wakeResult = runWakeAndRecapture(intent, tempDir, captureDir, &tempFiles, &recaptureInfo)
		}
	}

	// 8. 清理宿主机临时文件（在打包前完成，清理结果写入 capture-result.json；失败只记录不阻断）。
	cleanup := cleanupTemp()

	// 9. 写 capture-result.json（质量统计、唤醒决定与固定操作结果）。
	result := vmConsoleCaptureResult{
		Executor:         executorVmConsoleCapture,
		OperationVersion: intent.OperationVersion,
		CaptureMode:      intent.CaptureMode,
		HostNodeID:       intent.HostNodeID,
		VMID:             intent.VMID,
		Baseline: vmConsoleCaptureFileInfo{
			CapturedAt: baselineCapturedAt,
			ExitCode:   baselineExit,
			SHA256:     hex.EncodeToString(baselineHash[:]),
			SizeBytes:  int64(len(baselineRaw)),
		},
		Quality: vmConsoleQualityResult{
			AlgorithmRevision: quality.AlgorithmRevision,
			ParseOK:           quality.ParseOK,
			ParseError:        quality.ParseError,
			NearBlack:         quality.NearBlack,
			Metrics:           quality.Metrics,
		},
		WakeDecision:     wakeDecision,
		WakeResult:       wakeResult,
		Recapture:        recaptureInfo,
		Cleanup:          cleanup,
		SourceSignalRefs: intent.SourceSignalRefs,
	}
	resultBytes, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return fail(1, "vm_console_result_marshal_failed: "+err.Error())
	}
	if err := os.WriteFile(filepath.Join(captureDir, "capture-result.json"), append(resultBytes, '\n'), 0o600); err != nil {
		return fail(1, "vm_console_result_write_failed: "+err.Error())
	}

	fmt.Printf("  - 虚拟机控制台采集项 %s 完成：近黑=%v 唤醒决定=%s\n", item.ItemID, quality.NearBlack, wakeDecision)
	zero := 0
	return executionRow{ItemID: item.ItemID, CollectorID: item.CollectorID, ExitCode: &zero, Status: "success"}
}

// runWakeAndRecapture 执行固定的唤醒操作（sendkey down）与一次重截图。
// 每个 case + vm + run 最多一次唤醒（由采集项粒度天然保证：一个采集项只执行一次）。
func runWakeAndRecapture(intent captureIntent, tempDir, captureDir string, tempFiles *[]string, recaptureInfo **vmConsoleCaptureFileInfo) string {
	// 固定唤醒操作：sendkey down（仅此一种，不支持键名、组合键或任意 Monitor 文本）。
	if _, err := runVtpshMonitorCommand(intent, "sendkey down", intent.TimeoutSeconds); err != nil {
		fmt.Printf("  - 固定唤醒操作失败：%v\n", err)
		return wakeResultSendkeyFailed
	}
	// 等待短暂、固定且有上限的稳定窗口，再采集第二张截图；不得循环重试。
	time.Sleep(vmConsoleWakeSettleWindow)
	wakeCaptureID, err := newUUID()
	if err != nil {
		return wakeResultRecaptureFailed
	}
	recaptureTempPath := filepath.Join(tempDir, wakeCaptureID+".ppm")
	*tempFiles = append(*tempFiles, recaptureTempPath)
	recaptureDeadline := time.Now().Add(time.Duration(intent.TimeoutSeconds) * time.Second)
	recaptureExit, recaptureErr := runVtpshScreendump(intent, recaptureTempPath, intent.TimeoutSeconds)
	if recaptureErr != nil {
		fmt.Printf("  - 唤醒后重截图失败：%v\n", recaptureErr)
		return wakeResultRecaptureFailed
	}
	if err := waitForFile(recaptureTempPath, recaptureDeadline); err != nil {
		fmt.Printf("  - 唤醒后重截图超时：%v\n", err)
		return wakeResultRecaptureFailed
	}
	recaptureRaw, err := readCaptureFile(recaptureTempPath, intent.MaxCaptureBytes)
	if err != nil {
		fmt.Printf("  - 唤醒后重截图读取失败：%v\n", err)
		return wakeResultRecaptureFailed
	}
	if err := os.WriteFile(filepath.Join(captureDir, "recapture-after-wake.ppm"), recaptureRaw, 0o600); err != nil {
		fmt.Printf("  - 唤醒后重截图写入失败：%v\n", err)
		return wakeResultRecaptureFailed
	}
	recaptureHash := sha256.Sum256(recaptureRaw)
	*recaptureInfo = &vmConsoleCaptureFileInfo{
		CapturedAt: time.Now().UTC().Format(time.RFC3339Nano),
		ExitCode:   recaptureExit,
		SHA256:     hex.EncodeToString(recaptureHash[:]),
		SizeBytes:  int64(len(recaptureRaw)),
	}
	return wakeResultSuccess
}

// defaultVmConsoleWakeConfirm 是生产唤醒确认实现：
//   - 非交互模式（--non-interactive）→ non_interactive，绝不发键；
//   - 无交互 TTY → non_interactive，绝不发键；
//   - 否则在本机 TTY 提示人工确认，超时按拒绝处理。
//
// 不存在任何命令行参数、KBD 字段或环境变量能把唤醒预先设为"自动同意"。
func defaultVmConsoleWakeConfirm(nonInteractive bool) (bool, string) {
	if nonInteractive {
		return false, wakeDecisionNonInteractive
	}
	if !hasInteractiveTTY() {
		return false, wakeDecisionNonInteractive
	}
	decision := promptWakeConfirmation(bufio.NewReader(os.Stdin))
	return decision == wakeDecisionConfirmed, decision
}

// hasInteractiveTTY 判断是否存在可交互 TTY（纯标准库实现，无 CGO、无新增模块依赖）。
func hasInteractiveTTY() bool {
	info, err := os.Stdin.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return false
	}
	file, err := os.OpenFile("/dev/tty", os.O_RDWR, 0)
	if err != nil {
		return false
	}
	_ = file.Close()
	return true
}

// promptWakeConfirmation 在 TTY 中读取操作员 y/N 决定；超时视为拒绝（fail-closed）。
// 确认文案必须说明"将向该虚拟机控制台发送一次向下方向键，可能改变当前界面选择"。
// 注意：超时后后台读取 goroutine 会残留至进程结束；采集器为一次性进程，可接受。
func promptWakeConfirmation(reader *bufio.Reader) string {
	fmt.Println("  首张控制台截图接近黑屏，虚拟机控制台可能已熄屏。")
	fmt.Print("  将向该虚拟机控制台发送一次向下方向键尝试唤醒后重新截图，可能改变当前界面选择。确认执行？[y/N]: ")
	lines := make(chan string, 1)
	go func() {
		line, _ := reader.ReadString('\n')
		lines <- line
	}()
	select {
	case line := <-lines:
		answer := strings.ToLower(strings.TrimSpace(line))
		if answer == "y" || answer == "yes" {
			return wakeDecisionConfirmed
		}
		return wakeDecisionDeclined
	case <-time.After(vmConsoleWakePromptTimeout):
		fmt.Println("  唤醒确认超时，不发送按键，保留基线截图。")
		return wakeDecisionTimedOut
	}
}

func loadExecutionRows(outputDir string) ([]executionRow, collectionStats, error) {
	path := filepath.Join(outputDir, "execution-manifest.jsonl")
	file, err := os.Open(path)
	if err != nil {
		return nil, collectionStats{}, fmt.Errorf("结构化采集器未生成执行清单：%s", path)
	}
	defer file.Close()
	rows := make([]executionRow, 0)
	stats := collectionStats{}
	fmt.Println("  采集结果：")
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		if scanner.Text() == "" {
			continue
		}
		var row executionRow
		if err := json.Unmarshal(scanner.Bytes(), &row); err != nil {
			return nil, collectionStats{}, fmt.Errorf("执行清单不是合法 JSONL：%w", err)
		}
		rows = append(rows, row)
		state := ""
		if row.Status == "awaiting_manual_attachment" {
			stats.Manual++
			state = "待人工附件"
		} else if row.ExitCode != nil && *row.ExitCode == 0 {
			stats.Success++
			state = "成功"
		} else {
			stats.Failed++
			state = "失败"
			if row.ExitCode != nil {
				state = fmt.Sprintf("失败(exit=%d)", *row.ExitCode)
			}
		}
		fmt.Printf("  - %s  %s\n", row.CollectorID, state)
	}
	if err := scanner.Err(); err != nil {
		return nil, collectionStats{}, err
	}
	if stats.Failed > 0 {
		fmt.Printf("  警告：%d 项采集失败，将以 failed 状态进入证据包；必要时请补采。\n", stats.Failed)
	}
	return rows, stats, nil
}
