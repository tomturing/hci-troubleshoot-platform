package main

import (
	"strings"
	"testing"
)

func TestPickSpcDirRejectsAmbiguity(t *testing.T) {
	if _, err := pickSpcDir(""); err == nil {
		t.Fatal("空输出必须报错")
	}
	if _, err := pickSpcDir("/sf/a/support_bundle_cluster/spc\n/sf/b/support_bundle_cluster/spc\n"); err == nil {
		t.Fatal("多个目录必须拒绝猜测")
	}
	got, err := pickSpcDir("  /sf/data/local/x/support_bundle_cluster/spc  \n")
	if err != nil || got != "/sf/data/local/x/support_bundle_cluster/spc" {
		t.Fatalf("单目录解析失败：%q %v", got, err)
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

func TestBuildSpcWriteCommandUsesBase64(t *testing.T) {
	cmd := buildSpcWriteCommand("/sf/spc/core.json", "{\"a\":1}")
	if !strings.Contains(cmd, "base64 -d") {
		t.Fatalf("必须用 base64 写入：%s", cmd)
	}
	if !strings.Contains(cmd, "core.json") {
		t.Fatalf("目标路径丢失：%s", cmd)
	}
}
