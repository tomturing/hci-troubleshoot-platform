package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Fixture 对应方案 8 节 agent_test_fixture 的最小可运行子集。
// 仅 status == "published"（或留空）的 fixture 才会被路由执行。
type Fixture struct {
	FixtureID           string `json:"fixture_id"`
	ScenarioID          string `json:"scenario_id,omitempty"`
	KbdID               string `json:"kbd_id,omitempty"`
	KbdRevision         string `json:"kbd_revision,omitempty"`
	AcquisitionKey      string `json:"acquisition_key,omitempty"`
	SignalID            string `json:"signal_id,omitempty"`
	Tool                string `json:"tool,omitempty"`       // qfk_log / qfk_system / ...
	ResourceKeyword     string `json:"resource_keyword,omitempty"`
	Variant             string `json:"variant,omitempty"`    // positive/negative/near_miss/error/timeout
	CommandMatch        string `json:"command_match,omitempty"` // 正则，匹配命令原始字符串
	TargetHost          string `json:"target_host,omitempty"`
	Container           string `json:"container,omitempty"`
	ExitCode            int    `json:"exit_code"`
	Stdout              string `json:"stdout,omitempty"`
	Stderr              string `json:"stderr,omitempty"`
	DelayProfileMS      int    `json:"delay_profile_ms,omitempty"`
	ChunkProfileMS      int    `json:"chunk_profile_ms,omitempty"`
	Timeout             bool   `json:"timeout,omitempty"`    // 模拟命令超时（不返回 exit-status）
	ErrorType           string `json:"error_type,omitempty"`
	FixtureManifestHash string `json:"fixture_manifest_hash,omitempty"`
	Status              string `json:"status,omitempty"` // draft/validated/published/retired
}

// loadFixtures 从目录（*.json）或单个文件加载 fixture，过滤掉非 published 的条目。
func loadFixtures(path string) ([]*Fixture, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("fixtures 路径不可访问: %w", err)
	}
	var files []string
	if info.IsDir() {
		entries, err := os.ReadDir(path)
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			if !e.IsDir() && strings.HasSuffix(e.Name(), ".json") {
				files = append(files, filepath.Join(path, e.Name()))
			}
		}
	} else {
		files = []string{path}
	}

	var fixtures []*Fixture
	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		var list []Fixture
		if err := json.Unmarshal(data, &list); err != nil {
			// 兼容单对象 fixture 文件
			var single Fixture
			if err2 := json.Unmarshal(data, &single); err2 != nil {
				return nil, fmt.Errorf("解析 fixture %s 失败: %w", f, err)
			}
			list = append(list, single)
		}
		for i := range list {
			fx := list[i]
			if fx.Status != "" && fx.Status != "published" {
				continue
			}
			fixtures = append(fixtures, &fx)
		}
	}
	return fixtures, nil
}
