package main

import (
	"strings"
	"testing"
)

func TestPickSpcDirFixedPathSemantics(t *testing.T) {
	if _, err := pickSpcDir(""); err == nil {
		t.Fatal("空输出必须报错")
	}
	// 固定路径方案下多目录不再报歧义，只认 acli 插件目录下的那一个。
	if _, err := pickSpcDir("/sf/a/support_bundle_cluster/spc\n/sf/b/support_bundle_cluster/spc\n"); err == nil {
		t.Fatal("不含固定路径的输出必须报错")
	}
	got, err := pickSpcDir("  " + spcDeployDir + "  \n")
	if err != nil || got != spcDeployDir {
		t.Fatalf("固定路径解析失败：%q %v", got, err)
	}
	// 目录真实不存在时返回稳定错误码，不再有 ambiguous。
	if dir, code := spcResolveDir(""); code != spcErrDirMissing || dir != "" {
		t.Fatalf("空输出应给目录缺失码：dir=%q code=%q", dir, code)
	}
}

func TestSpcFilePathRejectsBadSlot(t *testing.T) {
	if _, err := spcFilePath("/sf/spc", "../etc"); err == nil {
		t.Fatal("非法槽位必须拒绝")
	}
	got, err := spcFilePath("/sf/spc", "core")
	if err != nil || got != "/sf/spc/core.json" {
		t.Fatalf("路径拼接错误：%q %v", got, err)
	}
}

func TestSpcBackupPathRequiresTaskID(t *testing.T) {
	if _, err := spcBackupPath("/sf/spc/core.json", "a/b"); err == nil {
		t.Fatal("非法任务 ID 必须拒绝")
	}
	got, err := spcBackupPath("/sf/spc/core.json", "task-1")
	if err != nil || got != "/sf/spc/core.json.platform.task-1.bak" {
		t.Fatalf("备份路径错误：%q %v", got, err)
	}
}

func TestValidateSpcContent(t *testing.T) {
	valid := "[{\"note\":\"1.白盒日志\"},{\"action\":\"copy\",\"path\":\"/sf/log/{date}.tar.gz\"}]"
	if err := validateSpcContent(valid); err != nil {
		t.Fatalf("合法内容被拒：%v", err)
	}
	cases := map[string]string{
		"非法 JSON":   "{",
		"未知 action": "[{\"action\":\"rm\",\"path\":\"/sf\"}]",
		"空 path":    "[{\"action\":\"copy\",\"path\":\"\"}]",
		"空数组":       "[]",
	}
	for name, raw := range cases {
		if err := validateSpcContent(raw); err == nil {
			t.Fatalf("%s 必须被拒绝", name)
		}
	}
}

func TestBuildSpcWriteCommandUsesPrintfNoBase64(t *testing.T) {
	cmd := buildSpcWriteCommand("/sf/spc/core.json", "{\"a\":1}")
	if strings.Contains(cmd, "base64") {
		t.Fatalf("HCI 宿主机没有 base64，写入不能依赖它：%s", cmd)
	}
	if !strings.Contains(cmd, "printf '%s'") {
		t.Fatalf("应使用 shell 内建 printf 直写：%s", cmd)
	}
	if !strings.Contains(cmd, "core.json") {
		t.Fatalf("目标路径丢失：%s", cmd)
	}
}

// 内容含单引号、$、* 时，单引号包裹必须仍然安全（不能被 shell 展开/截断）。
func TestBuildSpcWriteCommandQuotesSpecialChars(t *testing.T) {
	content := `[{"note":"a'b","path":"/sf/$HOME/*"}]`
	cmd := buildSpcWriteCommand("/sf/spc/core.json", content)
	if strings.Contains(cmd, "base64") {
		t.Fatalf("不能依赖 base64：%s", cmd)
	}
	if strings.Contains(cmd, "a'b") {
		t.Fatalf("单引号未转义，会被 shell 截断：%s", cmd)
	}
	if !strings.Contains(cmd, `a'\''b`) {
		t.Fatalf("单引号转义不符合 POSIX：%s", cmd)
	}
}

func TestPickRestorableBackupPrefersNewest(t *testing.T) {
	target := "/sf/spc/core.json"
	out := "/sf/spc/core.json.platform.task-b.bak\n/sf/spc/core.json.platform.task-a.bak\n"
	got, err := pickRestorableBackup(out, target)
	if err != nil || got != "/sf/spc/core.json.platform.task-b.bak" {
		t.Fatalf("应取第一项（mtime 最新）：%q %v", got, err)
	}
	if _, err := pickRestorableBackup("/sf/spc/other.json.platform.x.bak\n", target); err == nil {
		t.Fatal("不匹配的备份必须跳过")
	}
}

func TestBuildSpcBackupListCommand(t *testing.T) {
	cmd, err := buildSpcBackupListCommand("/sf/spc", "core")
	if err != nil || !strings.Contains(cmd, "ls -1t") || !strings.Contains(cmd, "core.json.platform.*.bak") {
		t.Fatalf("命令不对：%s %v", cmd, err)
	}
	if _, err := buildSpcBackupListCommand("/sf/sp c", "core"); err == nil {
		t.Fatal("含空格的目录必须拒绝（glob 会被拆开）")
	}
}
