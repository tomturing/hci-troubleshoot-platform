package main

// 自定义场景（-c）节点侧落地的护栏测试。
//
// 这些用例守的是"静默失败"：acli 解析失败/路径不存在都不报错，
// 因此 bridge 必须在下发前把配置内容、字段集合、路径字符全部验一遍。

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

func TestParseLogCollectConfig(t *testing.T) {
	t.Run("字面路径按逗号拆分并去重", func(t *testing.T) {
		paths, err := parseLogCollectConfig(`{"paths":"/sf/log/a/,/sf/etc/b.conf,/sf/log/a/"}`)
		if err != nil {
			t.Fatalf("不应拒绝：%v", err)
		}
		if len(paths) != 2 || paths[0] != "/sf/log/a/" || paths[1] != "/sf/etc/b.conf" {
			t.Fatalf("路径解析错误：%v", paths)
		}
	})

	t.Run("只允许 paths 一个键", func(t *testing.T) {
		if _, err := parseLogCollectConfig(`{"paths":"/sf/a","modules":["core"]}`); err == nil {
			t.Fatal("modules 永不生效，出现即应拒绝，避免误以为专项被覆盖")
		}
		if _, err := parseLogCollectConfig(`{"version":"1","paths":"/sf/a"}`); err == nil {
			t.Fatal("version 字段应被拒绝")
		}
	})

	t.Run("非法 JSON 与空配置", func(t *testing.T) {
		if _, err := parseLogCollectConfig(`{"paths":`); err == nil {
			t.Fatal("非法 JSON 必须拒绝")
		}
		if _, err := parseLogCollectConfig(``); err == nil {
			t.Fatal("空配置必须拒绝")
		}
	})

	t.Run("路径白名单：相对路径/穿越/空格/分号/通配符", func(t *testing.T) {
		for _, bad := range []string{
			"sf/relative", "/sf/../etc/shadow", "/sf/a b", "/sf/a;id", "/sf/a*", "/sf/a,b",
		} {
			raw := `{"paths":"` + bad + `"}`
			if bad == "/sf/a,b" {
				// 逗号会被拆成两条：其中 sf 前面的 "/sf/a" 合法，
				// 但第二条 "b" 不是绝对路径，仍必须被拒绝。
			}
			if _, err := parseLogCollectConfig(raw); err == nil {
				t.Fatalf("非法路径未被拒绝：%s", bad)
			}
		}
	})

	t.Run("超大配置", func(t *testing.T) {
		if _, err := parseLogCollectConfig(`{"paths":"/sf/` + strings.Repeat("a", logCollectConfigMaxBytes) + `"}`); err == nil {
			t.Fatal("超大配置必须拒绝")
		}
	})
}

func TestVerifyConfigSHA256(t *testing.T) {
	raw := `{"paths":"/sf/log/a"}`
	sum := sha256.Sum256([]byte(raw))
	if err := verifyConfigSHA256(raw, hex.EncodeToString(sum[:])); err != nil {
		t.Fatalf("正确摘要不应拒绝：%v", err)
	}
	if err := verifyConfigSHA256(raw, strings.Repeat("0", 64)); err == nil {
		t.Fatal("摘要不一致必须拒绝：否则写坏了也照常下发")
	}
	if err := verifyConfigSHA256(raw, ""); err == nil {
		t.Fatal("缺摘要必须拒绝")
	}
}

func TestLogCollectConfigPath(t *testing.T) {
	if got := logCollectConfigPath("task-123"); !strings.HasSuffix(got, "/cfg-task-123.json") {
		t.Fatalf("路径不符合约定：%s", got)
	}
	// task_id 会进文件路径，而路径会进 shell：注入形态必须被拒
	for _, bad := range []string{"", "a;id", "../../etc/x", "a b"} {
		if got := logCollectConfigPath(bad); got != "" {
			t.Fatalf("非法 task_id 应拒绝：%q → %q", bad, got)
		}
	}
	if logCollectConfigPath("task-1") == logCollectConfigPath("task-2") {
		t.Fatal("不同任务必须落在不同文件，避免配置互相覆盖")
	}
}
