package main

import (
	"archive/zip"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"hci_sim/internal/fixture"
)

type offlineArtifact struct {
	ExecutionItems []offlineExecutionItem `json:"execution_items"`
}

type offlineExecutionItem struct {
	ItemID           string                   `json:"item_id"`
	CollectorID      string                   `json:"collector_id"`
	SourceSignalRefs []offlineSourceSignalRef `json:"source_signal_refs"`
	Executor         string                   `json:"executor"`
	Argv             []string                 `json:"argv"`
}

type offlineSourceSignalRef struct {
	SupportID string `json:"support_id"`
	SignalID  string `json:"signal_id"`
}

type scenarioRouteResult struct {
	argv   []string
	result fixture.Result
}

func runOfflineManifest(args []string) error {
	flags := flag.NewFlagSet("offline-manifest", flag.ContinueOnError)
	verificationBundle := flags.String("verification-bundle", "", "客户下载的 Verification Bundle ZIP")
	onlineManifest := flags.String("online-manifest", "", "同一场景的在线 fixture manifest")
	outputPath := flags.String("output", "", "离线本地 aCLI fixture manifest 输出路径")
	variant := flags.String("variant", "positive", "场景变体")
	node := flags.String("virtual-node", "SIM-HCI-NODE-01", "虚拟节点")
	container := flags.String("container", "host", "目标容器")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *verificationBundle == "" || *onlineManifest == "" || *outputPath == "" {
		return errors.New("必须指定 --verification-bundle、--online-manifest 和 --output")
	}
	router, err := fixture.Load(*onlineManifest)
	if err != nil {
		return err
	}
	artifact, err := readOfflineArtifact(*verificationBundle)
	if err != nil {
		return err
	}
	baseResults := make([]scenarioRouteResult, 0, len(router.Routes()))
	for _, route := range router.Routes() {
		result, matchErr := router.MatchArgv(route.RouteKey.Argv, *variant, route.RouteKey.Node, route.RouteKey.Container)
		if matchErr != nil {
			return matchErr
		}
		baseResults = append(baseResults, scenarioRouteResult{argv: route.RouteKey.Argv, result: result})
	}
	routes := make([]fixture.Route, 0, len(artifact.ExecutionItems))
	for index, item := range artifact.ExecutionItems {
		if item.Executor != "command" || len(item.Argv) == 0 || item.Argv[0] != "acli" {
			continue
		}
		result, ok := matchOfflineResult(router.KBD().SupportID, item.SourceSignalRefs, item.Argv, baseResults)
		if !ok {
			return fmt.Errorf("离线采集项 %s 无法映射到场景 Signal 输出", item.CollectorID)
		}
		argv, normalizeErr := fixture.NormalizeArgv(item.Argv)
		if normalizeErr != nil {
			return fmt.Errorf("离线采集项 %s argv 无效: %w", item.ItemID, normalizeErr)
		}
		routes = append(routes, fixture.Route{
			ID:       fmt.Sprintf("offline-%03d-%s", index+1, item.ItemID),
			SignalID: result.SignalID,
			Variant:  *variant,
			RouteKey: fixture.RouteKey{
				Tool: argv[0], AcquisitionKey: acquisitionForArgv(argv), Argv: argv,
				Node: *node, Container: *container,
			},
			Result: fixture.ResultDef{ExitCode: result.ExitCode, Stdout: result.Stdout, Stderr: result.Stderr},
			Fault:  result.Fault,
		})
	}
	if len(routes) == 0 {
		return errors.New("Verification Bundle 不包含可由实验室执行的 aCLI 采集项")
	}
	manifest := fixture.Manifest{
		SchemaVersion: fixture.SchemaVersion,
		Bundle:        fixture.BundleRef{Status: "published"},
		KBD:           router.KBD(), Contracts: router.Contracts(),
		Variables: map[string]string{"SYNTHETIC": "true", "FACTS_BOUNDARY": "offline-verification-bundle"},
		Limits:    fixture.Limits{MaxRoutes: len(routes), MaxOutputBytesPerCommand: router.OutputLimit(), MaxBundleBytes: 4 * 1024 * 1024},
		Routes:    routes,
	}
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	raw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	if _, err := fixture.Parse(raw); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(*outputPath), 0700); err != nil {
		return err
	}
	return os.WriteFile(*outputPath, append(raw, '\n'), 0600)
}

func readOfflineArtifact(bundlePath string) (offlineArtifact, error) {
	archive, err := zip.OpenReader(bundlePath)
	if err != nil {
		return offlineArtifact{}, fmt.Errorf("打开 Verification Bundle 失败: %w", err)
	}
	defer archive.Close()
	var artifactName string
	for _, file := range archive.File {
		if file.Name != "artifact-manifest.json" {
			continue
		}
		reader, openErr := file.Open()
		if openErr != nil {
			return offlineArtifact{}, openErr
		}
		var manifest struct {
			FileName string `json:"file_name"`
		}
		decodeErr := json.NewDecoder(reader).Decode(&manifest)
		reader.Close()
		if decodeErr != nil || filepath.Base(manifest.FileName) != manifest.FileName {
			return offlineArtifact{}, errors.New("artifact-manifest.json 缺少合法 file_name")
		}
		artifactName = manifest.FileName
		break
	}
	if artifactName == "" {
		return offlineArtifact{}, errors.New("Verification Bundle 缺少 artifact-manifest.json")
	}
	for _, file := range archive.File {
		if file.Name != artifactName {
			continue
		}
		reader, openErr := file.Open()
		if openErr != nil {
			return offlineArtifact{}, openErr
		}
		defer reader.Close()
		limited := io.LimitReader(reader, 8*1024*1024)
		var artifact offlineArtifact
		if err := json.NewDecoder(limited).Decode(&artifact); err != nil {
			return offlineArtifact{}, fmt.Errorf("解析结构化采集制品失败: %w", err)
		}
		return artifact, nil
	}
	return offlineArtifact{}, errors.New("Verification Bundle 缺少结构化采集制品")
}

func matchOfflineResult(supportID string, sourceRefs []offlineSourceSignalRef, offlineArgv []string, results []scenarioRouteResult) (fixture.Result, bool) {
	// 新制品将 KBD/Signal 来源固化在签名正文中。命令语义匹配只用于兼容旧制品。
	if len(sourceRefs) > 0 {
		allowed := make(map[string]bool, len(sourceRefs))
		for _, source := range sourceRefs {
			if source.SupportID == supportID {
				allowed[source.SignalID] = true
			}
		}
		var exact fixture.Result
		for _, candidate := range results {
			if !allowed[candidate.result.SignalID] {
				continue
			}
			if exact.FixtureID != "" {
				return fixture.Result{}, false
			}
			exact = candidate.result
		}
		return exact, exact.FixtureID != ""
	}
	offlineKey := commandSemanticKey(offlineArgv)
	if offlineKey == "" {
		return fixture.Result{}, false
	}
	var best fixture.Result
	ambiguous := false
	for _, candidate := range results {
		if commandSemanticKey(candidate.argv) != offlineKey {
			continue
		}
		if best.FixtureID == "" {
			best, ambiguous = candidate.result, false
		} else {
			ambiguous = true
		}
	}
	return best, best.FixtureID != "" && !ambiguous
}

func commandSemanticKey(argv []string) string {
	if len(argv) < 2 || argv[0] != "acli" {
		return ""
	}
	globalWithValue := map[string]bool{"--formatter": true, "--timeout": true, "--container": true}
	globalFlags := map[string]bool{"--cluster": true}
	index := 1
	for index < len(argv) {
		if globalFlags[argv[index]] {
			index++
			continue
		}
		if globalWithValue[argv[index]] && index+1 < len(argv) {
			index += 2
			continue
		}
		break
	}
	if index >= len(argv) {
		return ""
	}
	namespace := argv[index]
	if namespace == "log" || namespace == "alert" || namespace == "task" {
		key := namespace + ":get"
		for optionIndex := index + 1; optionIndex+1 < len(argv); optionIndex++ {
			if argv[optionIndex] == "-k" || argv[optionIndex] == "-f" || argv[optionIndex] == "-p" {
				key += ":" + argv[optionIndex] + "=" + argv[optionIndex+1]
				optionIndex++
			}
		}
		return key
	}
	if namespace == "service" && index+3 < len(argv) {
		return "service:" + argv[index+1] + ":" + argv[index+2] + ":" + argv[index+3]
	}
	if index+1 >= len(argv) {
		return namespace
	}
	commandParts := []string{namespace}
	for _, value := range argv[index+1:] {
		if strings.HasPrefix(value, "-") {
			break
		}
		commandParts = append(commandParts, value)
	}
	return strings.Join(commandParts, ":")
}

func acquisitionForArgv(argv []string) string {
	if len(argv) < 2 {
		return argv[0]
	}
	return argv[0] + ":" + argv[1]
}
