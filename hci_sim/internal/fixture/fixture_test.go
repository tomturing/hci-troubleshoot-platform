package fixture

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const manifestJSON = `{
  "schema_version":"1.0",
  "kbd":{"support_id":"27123","revision":1,"checksum":"abc"},
  "variables":{"VM":"271230001","PID":"9527"},
  "routes":[
    {"id":"sig2-positive","signal_id":"sig_002","variant":"positive-realistic","command_pattern":"acli system lsof","exit_code":0,"stdout":"flock {{PID}} root /{{VM}}.vm/disk.qcow2\n"},
    {"id":"sig2-negative","signal_id":"sig_002","variant":"negative","command_pattern":"acli system lsof","exit_code":0,"stdout":""}
  ]
}`

func TestRouterSeparatesVariantsAndRendersVariables(t *testing.T) {
	router, err := Parse([]byte(manifestJSON))
	if err != nil {
		t.Fatal(err)
	}
	positive, err := router.Match("  acli   system lsof ", "positive-realistic")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(positive.Stdout, "9527") || !strings.Contains(positive.Stdout, "271230001") {
		t.Fatalf("变量未渲染: %q", positive.Stdout)
	}
	negative, err := router.Match("acli system lsof", "negative")
	if err != nil || negative.Stdout != "" {
		t.Fatalf("negative 路由错误: %+v err=%v", negative, err)
	}
}

func TestRouterFailsClosed(t *testing.T) {
	router, err := Parse([]byte(manifestJSON))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := router.Match("uname -a", "positive-realistic"); err == nil || err.Error() != "fixture_not_found" {
		t.Fatalf("未知命令未 fail closed: %v", err)
	}
	for _, command := range []string{"acli system lsof; id", "acli system lsof | cat", "acli\nsystem lsof"} {
		if _, err := router.Match(command, "positive-realistic"); err == nil || !strings.Contains(err.Error(), "policy_denied") {
			t.Fatalf("危险命令未拒绝: %q err=%v", command, err)
		}
	}
}

func TestDeploymentFixtureMatchesGoldenTestdata(t *testing.T) {
	goldenPath := filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json")
	deploymentPath := filepath.Join("..", "..", "..", "deploy", "helm", "hci-sim", "files", "kbd-27123-fixture-manifest.json")
	golden, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("读取 Golden Fixture 失败: %v", err)
	}
	deployment, err := os.ReadFile(deploymentPath)
	if err != nil {
		t.Fatalf("读取部署 Fixture 失败: %v", err)
	}
	if !bytes.Equal(golden, deployment) {
		t.Fatal("测试与部署 Fixture 已漂移，必须同时更新")
	}
}

func TestGoldenFixtureVariantsRemainIsolated(t *testing.T) {
	manifestPath := filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json")
	router, err := Load(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	command := "acli system lsof"
	tests := []struct {
		variant     string
		wantExit    int
		wantContent string
		wantDelay   int
	}{
		{variant: "positive-realistic", wantExit: 0, wantContent: "9527"},
		{variant: "negative", wantExit: 0, wantContent: ""},
		{variant: "near-miss", wantExit: 0, wantContent: "OTHER-VM"},
		{variant: "permission", wantExit: 13, wantContent: "Permission denied"},
		{variant: "timeout", wantExit: 0, wantDelay: 180000},
	}
	for _, test := range tests {
		result, err := router.Match(command, test.variant)
		if err != nil {
			t.Fatalf("variant=%s 匹配失败: %v", test.variant, err)
		}
		contentMatches := strings.Contains(strings.ToLower(result.Stdout+result.Stderr), strings.ToLower(test.wantContent))
		if result.ExitCode != test.wantExit || result.DelayMS != test.wantDelay || !contentMatches {
			t.Fatalf("variant=%s 隔离结果错误: %+v", test.variant, result)
		}
	}
}
