package main

// 安装后逐节点插件落盘校验的单测：安装器退出码 0 不等于每台节点都装好，
// 这里的判定决定"分发假成功"会不会再次被掩盖（Q2026111700614）。

import (
	"strings"
	"testing"
)

func TestBuildPluginProbeCommand(t *testing.T) {
	command := buildPluginProbeCommand()
	if !strings.Contains(command, "[ -x "+shellQuote(logCollectPluginBinary)+" ]") {
		t.Fatalf("必须校验插件可执行（-x），空目录残骸不算落盘：%s", command)
	}
	if !strings.Contains(command, "printf 'OK") {
		t.Fatalf("落盘成功必须回显可判定的标记：%s", command)
	}
}

func TestParsePluginProbeResult(t *testing.T) {
	cases := map[string]bool{
		"OK\n":                true,
		"OK\r\n":              true,
		" OK \n":              true,
		"warning\nOK\n":       true,
		"":                    false,
		"OKAY\n":              false,
		"not OK\n":            false,
		"Permission denied\n": false,
	}
	for output, want := range cases {
		if got := parsePluginProbeResult(output); got != want {
			t.Fatalf("parsePluginProbeResult(%q) = %v，期望 %v", output, got, want)
		}
	}
}

func TestMissingPluginNodes(t *testing.T) {
	states := []nodePluginState{
		{IP: "10.0.0.1", Present: true},
		{IP: "10.0.0.2", Present: false, Detail: "ssh: connect refused"},
		{IP: "10.0.0.3", Present: true},
		{IP: "10.0.0.4", Present: false},
	}
	got := missingPluginNodes(states)
	if len(got) != 2 || got[0] != "10.0.0.2" || got[1] != "10.0.0.4" {
		t.Fatalf("未落盘节点汇总错误：%v", got)
	}
	if missing := missingPluginNodes(nil); len(missing) != 0 {
		t.Fatalf("无节点清单时不应报缺失：%v", missing)
	}
	allOK := []nodePluginState{{IP: "10.0.0.1", Present: true}}
	if missing := missingPluginNodes(allOK); len(missing) != 0 {
		t.Fatalf("全部落盘时不应报缺失：%v", missing)
	}
}
