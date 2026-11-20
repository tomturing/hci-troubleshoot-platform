package main

import (
	"strings"
	"testing"
)

// 内置脚本不全在 plugin 的 script/ 顶层：插件自带的一批脚本在 vs_script/ 等
// 子目录里。平台要覆写它们，code 就必须能表达相对子路径，否则这批脚本永远动不了。
func TestScriptFilePathAllowsRelativeSubdir(t *testing.T) {
	cases := []string{
		"dpcollect",
		"pl_disk_check.py",
		"vs_script/vs_collect_upgrade_info.py",
		"tools/sub/x.sh",
	}
	for _, code := range cases {
		got, err := scriptFilePath(scriptDeployDir, code)
		if err != nil {
			t.Fatalf("%s 应被接受，实际报错：%v", code, err)
		}
		want := scriptDeployDir + "/" + code
		if got != want {
			t.Fatalf("%s 路径拼接错误：%q != %q", code, got, want)
		}
	}
}

// 拒绝项都是"拼进 shell 命令后能改路径语义"的形态，必须一条都不放过。
func TestScriptFilePathRejectsTraversalAndNormalization(t *testing.T) {
	bad := []string{
		"",
		"/abs/x.py",
		"../x.py",
		"a/../b.py",
		"a//b.py",
		"a/",
		"a\\b.py",
		"a/b/c/d.py",
		".hidden",
		"a b.py",
		"x;id",
	}
	for _, code := range bad {
		if _, err := scriptFilePath(scriptDeployDir, code); err == nil {
			t.Fatalf("%q 应被拒绝", code)
		}
	}
}

// 目录自身被污染时（例如配置注入），也不允许拼出越界路径。
func TestScriptFilePathRejectsBadDir(t *testing.T) {
	for _, dir := range []string{"relative/dir", "/sf/../etc", ""} {
		if _, err := scriptFilePath(dir, "x.py"); err == nil {
			t.Fatalf("目录 %q 应被拒绝", dir)
		}
	}
}

func TestScriptFilePathKeepsDirPrefix(t *testing.T) {
	got, err := scriptFilePath(scriptDeployDir, "vs_script/a.py")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.HasPrefix(got, scriptDeployDir+"/") {
		t.Fatalf("路径必须落在 %s 下，实际 %q", scriptDeployDir, got)
	}
}
