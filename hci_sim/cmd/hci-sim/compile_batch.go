package main

// compile-batch 把 C1 权威快照中全部 ready_for_artifact_binding 的 KBD 一键编译为
// synthetic positive-minimal manifest。它与 bootstrap 复用同一编译内核
// buildSyntheticManifest，保证 digest、argv 规范化与输出模板逐字节一致；
// 它只做编译与本地校验，不签 lease、不写数据库、不改 GitOps 发布集合
// （values.yaml 由人工 PR 审查后合入，scripts/hci-sim/synthetic-batch-compile.sh 可代改）。

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"hci_sim/internal/fixture"
)

type capabilityListReport struct {
	Total        int                  `json:"total"`
	StatusCounts map[string]int       `json:"status_counts"`
	GapCounts    map[string]int       `json:"gap_counts"`
	Results      []capabilityResponse `json:"results"`
}

type batchCompiledEntry struct {
	SupportID string `json:"support_id"`
	Revision  int    `json:"kbd_revision"`
	Digest    string `json:"bundle_digest"`
	Routes    int    `json:"routes"`
	File      string `json:"file"`
}

type batchSkippedEntry struct {
	SupportID string   `json:"support_id"`
	Status    string   `json:"status"`
	GapCodes  []string `json:"gap_codes,omitempty"`
	Reason    string   `json:"reason,omitempty"`
}

type batchCompileReport struct {
	GeneratedAt     string               `json:"generated_at"`
	CapabilitiesURL string               `json:"capabilities_url"`
	SampleSuite     string               `json:"sample_suite,omitempty"`
	Total           int                  `json:"total"`
	StatusCounts    map[string]int       `json:"status_counts"`
	GapCounts       map[string]int       `json:"gap_counts"`
	Compiled        []batchCompiledEntry `json:"compiled"`
	Skipped         []batchSkippedEntry  `json:"skipped"`
	OutputDir       string               `json:"output_dir"`
	FactsBoundary   string               `json:"facts_boundary"`
}

func runCompileBatch(args []string) error {
	flags := flag.NewFlagSet("compile-batch", flag.ContinueOnError)
	capabilitiesURL := flags.String("capabilities-url", env("HCI_SIM_CAPABILITIES_URL", ""), "C1 capability 列表 API URL")
	apiToken := flags.String("api-token", env("INTERNAL_API_TOKEN", ""), "C1 internal API token")
	outputDir := flags.String("output-dir", "./.hci-sim-batch", "manifest 与批量报告输出目录")
	sampleSuite := flags.String("sample-suite", "", "仅编译该 sample_suite 的 KBD（空=全部）")
	node := flags.String("virtual-node", "SIM-HCI-NODE-01", "虚拟节点 ID")
	container := flags.String("container", "host", "目标容器")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*capabilitiesURL) == "" {
		return errors.New("必须提供 --capabilities-url 或 HCI_SIM_CAPABILITIES_URL；禁止脱离 C1 权威快照编译")
	}
	report, err := fetchCapabilityList(*capabilitiesURL, *apiToken, *sampleSuite)
	if err != nil {
		return err
	}
	now := time.Now().UTC().Truncate(time.Second)
	batch := batchCompileReport{
		GeneratedAt:     now.Format(time.RFC3339),
		CapabilitiesURL: strings.TrimRight(*capabilitiesURL, "/") + "/capabilities",
		SampleSuite:     strings.TrimSpace(*sampleSuite),
		Total:           report.Total,
		StatusCounts:    report.StatusCounts,
		GapCounts:       report.GapCounts,
		Compiled:        []batchCompiledEntry{},
		Skipped:         []batchSkippedEntry{},
		OutputDir:       *outputDir,
		FactsBoundary:   "synthetic positive-minimal 仅绑定已发布 Signal/Tool 契约，不代表真实 HCI 数据；不得据此宣称 E2E 已验证。",
	}
	if err := os.MkdirAll(*outputDir, 0o700); err != nil {
		return err
	}
	for _, item := range report.Results {
		if item.Status != "ready_for_artifact_binding" || item.Resolved == nil {
			batch.Skipped = append(batch.Skipped, skipFromGap(item))
			continue
		}
		resolved := item.Resolved
		if resolved.SupportID != item.SupportID || resolved.KBDRevision < 1 || strings.TrimSpace(resolved.KBDChecksum) == "" || strings.TrimSpace(resolved.SignalsDigest) == "" ||
			strings.TrimSpace(resolved.ToolContractRevision) == "" || strings.TrimSpace(resolved.PolicyRevision) == "" || len(resolved.SyntheticRoutes) == 0 {
			batch.Skipped = append(batch.Skipped, batchSkippedEntry{SupportID: item.SupportID, Status: item.Status, Reason: "C1 resolved 输入与请求 KBD 不一致或不完整"})
			continue
		}
		manifest, err := buildSyntheticManifest(resolved, *node, *container)
		if err != nil {
			batch.Skipped = append(batch.Skipped, batchSkippedEntry{SupportID: item.SupportID, Status: item.Status, Reason: err.Error()})
			continue
		}
		manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
		raw, err := json.MarshalIndent(manifest, "", "  ")
		if err != nil {
			return fmt.Errorf("序列化 KBD %s Bundle 失败: %w", item.SupportID, err)
		}
		if _, err := fixture.Parse(raw); err != nil {
			// 编译内核自校验失败属于工具缺陷而非数据缺口，整体失败关闭。
			return fmt.Errorf("KBD %s synthetic Bundle 自校验失败: %w", item.SupportID, err)
		}
		file := fmt.Sprintf("kbd-%s-synthetic-fixture-manifest.json", resolved.SupportID)
		if err := os.WriteFile(filepath.Join(*outputDir, file), append(raw, '\n'), 0o600); err != nil {
			return err
		}
		batch.Compiled = append(batch.Compiled, batchCompiledEntry{
			SupportID: resolved.SupportID, Revision: resolved.KBDRevision,
			Digest: manifest.Bundle.Digest, Routes: len(manifest.Routes), File: file,
		})
	}
	sort.Slice(batch.Compiled, func(i, j int) bool { return batch.Compiled[i].SupportID < batch.Compiled[j].SupportID })
	sort.Slice(batch.Skipped, func(i, j int) bool { return batch.Skipped[i].SupportID < batch.Skipped[j].SupportID })
	batchRaw, err := json.MarshalIndent(batch, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(*outputDir, "batch-report.json"), append(batchRaw, '\n'), 0o600); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "compile-batch: total=%d compiled=%d skipped=%d output=%s\n",
		batch.Total, len(batch.Compiled), len(batch.Skipped), *outputDir)
	for _, compiled := range batch.Compiled {
		fmt.Fprintf(os.Stderr, "  compiled %s rev=%d routes=%d digest=%s\n", compiled.SupportID, compiled.Revision, compiled.Routes, compiled.Digest)
	}
	for _, skipped := range batch.Skipped {
		reasons := append([]string{}, skipped.GapCodes...)
		if skipped.Reason != "" {
			reasons = append(reasons, skipped.Reason)
		}
		fmt.Fprintf(os.Stderr, "  skipped  %s status=%s gaps=%v\n", skipped.SupportID, skipped.Status, reasons)
	}
	return nil
}

func skipFromGap(item capabilityResponse) batchSkippedEntry {
	skipped := batchSkippedEntry{SupportID: item.SupportID, Status: item.Status, GapCodes: []string{}}
	for _, gap := range item.Gaps {
		skipped.GapCodes = append(skipped.GapCodes, gap.Code)
	}
	if len(skipped.GapCodes) == 0 {
		skipped.Reason = "尚未 ready_for_artifact_binding 且未报告 gap"
	}
	return skipped
}

func fetchCapabilityList(baseURL, token, sampleSuite string) (capabilityListReport, error) {
	target := strings.TrimRight(baseURL, "/") + "/capabilities"
	if strings.TrimSpace(sampleSuite) != "" {
		target += "?sample_suite=" + url.QueryEscape(strings.TrimSpace(sampleSuite))
	}
	req, err := http.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		return capabilityListReport{}, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return capabilityListReport{}, fmt.Errorf("读取 C1 capability 列表失败: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return capabilityListReport{}, fmt.Errorf("读取 C1 capability 列表失败: HTTP %d", resp.StatusCode)
	}
	var report capabilityListReport
	if err := json.NewDecoder(resp.Body).Decode(&report); err != nil {
		return capabilityListReport{}, fmt.Errorf("C1 capability 列表响应无效: %w", err)
	}
	return report, nil
}
