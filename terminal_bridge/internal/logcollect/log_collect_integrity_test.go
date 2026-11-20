package logcollect

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestHashFilePrefixStrictRebuildsDigest 续传摘要必须等于全量摘要。
//
// 差一个字节的偏移，得到的哈希就与源包基线对不上；而这种错只会在 20 GiB 传完
// 后才暴露，现场代价是整个包白拉一遍。因此在进程内把"已有部分 + 续传部分"的
// 组合结果钉死。
func TestHashFilePrefixStrictRebuildsDigest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "resume.bin")
	if err := os.WriteFile(path, []byte("0123456789"), 0o600); err != nil {
		t.Fatal(err)
	}

	hasher := sha256.New()
	if err := hashFilePrefixStrict(path, 4, hasher); err != nil {
		t.Fatalf("重建已有部分摘要失败: %v", err)
	}
	// 模拟 tail -c +5 续传剩下的 6 字节。
	if _, err := hasher.Write([]byte("456789")); err != nil {
		t.Fatal(err)
	}

	want := sha256.Sum256([]byte("0123456789"))
	if got := hex.EncodeToString(hasher.Sum(nil)); got != hex.EncodeToString(want[:]) {
		t.Fatalf("续传摘要与全量摘要不一致: got=%s want=%x", got, want)
	}
}

// TestHashFilePrefixStrictRejectsShortFile 本地残包不足时必须报错。
//
// 静默接受短读会让摘要只覆盖已有字节，最终"校验通过"的是一个更小的包。
func TestHashFilePrefixStrictRejectsShortFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "short.bin")
	if err := os.WriteFile(path, []byte("abc"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := hashFilePrefixStrict(path, 10, sha256.New()); err == nil {
		t.Fatal("残包长度不足必须报错")
	}
}

// TestFileDigest 摘要实现必须与标准值一致，否则整条链的比对基准都是错的。
func TestFileDigest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sample.bin")
	if err := os.WriteFile(path, []byte("hello"), 0o600); err != nil {
		t.Fatal(err)
	}
	md5Sum, err := FileDigest(path, "md5")
	if err != nil || md5Sum != "5d41402abc4b2a76b9719d911017c592" {
		t.Fatalf("md5 不符: %q err=%v", md5Sum, err)
	}
	shaSum, err := FileDigest(path, "sha256")
	if err != nil || shaSum != "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" {
		t.Fatalf("sha256 不符: %q err=%v", shaSum, err)
	}
}

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
