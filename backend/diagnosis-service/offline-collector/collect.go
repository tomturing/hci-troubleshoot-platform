package main

import (
	"bufio"
	"context"
	"crypto/tls"
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
	counts := map[string]int{"command": 0, "http": 0, "manual": 0}
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
	fmt.Printf("  采集项（共 %d 项：直接命令 %d / HCI API %d / 人工附件 %d）：\n", len(artifact.CollectionItems), counts["command"], counts["http"], counts["manual"])
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

func runCollection(artifact *collectorArtifact, outputDir string) error {
	fmt.Println("[3/4] 执行结构化采集制品 …")
	for _, relative := range []string{"commands", "manual-guides", "attachments"} {
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
