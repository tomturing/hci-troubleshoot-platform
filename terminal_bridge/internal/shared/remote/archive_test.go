package remote

// 摘要命令构造与解析的守卫。阶段 2 A3 之前它们住在 logcollect，
// 随 ProbeSourceDigest 一起下沉到 remote（交付域同样要取源包摘要）。

import (
	"os/exec"
	"strings"
	"testing"
)

// TestBuildSourceDigestCommandIsSyntacticallyValid 命令必须能被 POSIX sh 解析。
func TestBuildSourceDigestCommandIsSyntacticallyValid(t *testing.T) {
	command := buildSourceDigestCommand("/sf/data/local/acli/tmp/data/log_collect/Q2026102800001.zip")
	if err := exec.Command("/bin/sh", "-n", "-c", command).Run(); err != nil {
		t.Fatalf("摘要命令语法不合法: %v\n%s", err, command)
	}
}

// TestBuildSourceDigestCommandPrefersSHA256 顺序决定口径：先 sha256sum，再 md5sum，最后 acli。
func TestBuildSourceDigestCommandPrefersSHA256(t *testing.T) {
	command := buildSourceDigestCommand("/sf/data/local/acli/tmp/data/log_collect/Q2026102800001.zip")
	sha := strings.Index(command, "command -v sha256sum")
	md5 := strings.Index(command, "command -v md5sum")
	acli := strings.Index(command, "system md5sum")
	if sha < 0 || md5 < 0 || acli < 0 {
		t.Fatalf("缺少某个摘要分支: %s", command)
	}
	if !(sha < md5 && md5 < acli) {
		t.Fatalf("摘要优先级错误（应 sha256sum → md5sum → acli）: sha=%d md5=%d acli=%d", sha, md5, acli)
	}
}

// TestBuildSourceDigestCommandQuotesPath 路径必须始终以单引号形态出现，不能裸拼进 shell。
func TestBuildSourceDigestCommandQuotesPath(t *testing.T) {
	evil := "/sf/data/local/acli/tmp/data/log_collect/a b;rm -rf /tmp/x.zip"
	command := buildSourceDigestCommand(evil)
	quoted := ShellQuote(evil)
	if strings.Count(command, quoted) != 3 {
		t.Fatalf("路径应在三个分支中各被转义一次，实际 %d 次: %s", strings.Count(command, quoted), command)
	}
	if strings.Contains(command, " "+evil) {
		t.Fatalf("疑似存在未转义路径: %s", command)
	}
	if err := exec.Command("/bin/sh", "-n", "-c", command).Run(); err != nil {
		t.Fatalf("转义后命令语法不合法: %v", err)
	}
}

// TestParseSourceDigestOutput 解析必须严格到"算法与长度自洽"，且容忍前置噪声。
func TestParseSourceDigestOutput(t *testing.T) {
	sha := strings.Repeat("a", 64)
	md5 := strings.Repeat("b", 32)
	cases := []struct {
		name       string
		output     string
		wantAlgo   string
		wantDigest string
	}{
		{"sha256 正常", "sha256 " + sha, "sha256", sha},
		{"md5 正常", "md5 " + md5, "md5", md5},
		{"acli 归一为 md5", "acli " + md5, "md5", md5},
		{"前置噪声取最后有效行", "__ACLI_BIN__=/sf/bin/acli\nsha256sum: 提示\nsha256 " + sha, "sha256", sha},
		{"算法与长度不符不接受", "sha256 " + md5, "", ""},
		{"md5 长度不符不接受", "md5 " + sha, "", ""},
		{"空输出", "", "", ""},
		{"纯报错输出", "sha256sum: /path: No such file or directory", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			algo, digest, note := parseSourceDigestOutput(tc.output)
			if algo != tc.wantAlgo || digest != tc.wantDigest {
				t.Fatalf("解析不符：期望 (%q,%q)，实际 (%q,%q) note=%q", tc.wantAlgo, tc.wantDigest, algo, digest, note)
			}
			if algo == "" && note == "" {
				t.Fatal("解析失败时必须给出可读原因，否则排障只能猜")
			}
		})
	}
}
