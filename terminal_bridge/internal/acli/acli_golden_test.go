package acli

import (
	"os"
	"strconv"
	"strings"
	"testing"
)

// goldenShellQuote 是收口前 log_collect.go:187 的实现，逐字冻结（acli 包内独立基准）。
func goldenShellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

// goldenAcliWriteCommand 是收口前 acli_package.go:221 的实现，逐字冻结。
// 标记常量也冻结字面量，避免"常量改了、基准跟着改"的自证循环。
func goldenAcliWriteCommand(tmpPath, destPath string, size int64, sha256Hex, md5Hex, mode string) string {
	const (
		writeMark     = "__ACLI_WRITE "
		writeOKMark   = "__ACLI_WRITE_OK"
		writeFailMark = "__ACLI_WRITE_FAIL "
	)
	quotedTmp := goldenShellQuote(tmpPath)
	quotedDest := goldenShellQuote(destPath)
	if mode == "" {
		mode = "0755"
	}
	return "umask 077; " +
		"if cat > " + quotedTmp + "; then " +
		`SIZE=$(wc -c < ` + quotedTmp + ` 2>/dev/null | tr -d ' \r\n'); ` +
		`ALGO=size_only; DIG=""; ` +
		`if command -v sha256sum >/dev/null 2>&1; then ALGO=sha256; DIG=$(sha256sum ` + quotedTmp + ` 2>/dev/null | awk '{print $1}'); ` +
		`elif command -v md5sum >/dev/null 2>&1; then ALGO=md5; DIG=$(md5sum ` + quotedTmp + ` 2>/dev/null | awk '{print $1}'); fi; ` +
		`echo "` + writeMark + `size=${SIZE} algo=${ALGO} digest=${DIG}"; ` +
		`if [ "$SIZE" != "` + strconv.FormatInt(size, 10) + `" ]; then echo "` + writeFailMark + `size_mismatch"; rm -f ` + quotedTmp + `; exit 21; fi; ` +
		`if [ "$ALGO" = "sha256" ] && [ "$DIG" != "` + sha256Hex + `" ]; then echo "` + writeFailMark + `digest_mismatch"; rm -f ` + quotedTmp + `; exit 22; fi; ` +
		`if [ "$ALGO" = "md5" ] && [ "$DIG" != "` + md5Hex + `" ]; then echo "` + writeFailMark + `digest_mismatch"; rm -f ` + quotedTmp + `; exit 22; fi; ` +
		`if mv -f ` + quotedTmp + ` ` + quotedDest + ` && chmod ` + mode + ` ` + quotedDest + `; then echo "` + writeOKMark + `"; ` +
		`else echo "` + writeFailMark + `move_failed"; rm -f ` + quotedTmp + `; exit 23; fi; ` +
		`else echo "` + writeFailMark + `cat_failed"; rm -f ` + quotedTmp + `; exit 20; fi`
}

// goldenAcliProbeCommand 是收口前 acli_package.go:657 的实现，逐字冻结。
func goldenAcliProbeCommand(path string) string {
	const missingMark = "__ACLI_PROBE_MISSING"
	quoted := goldenShellQuote(path)
	return "if [ ! -s " + quoted + " ]; then echo \"" + missingMark + "\"; else " +
		`SIZE=$(wc -c < ` + quoted + ` 2>/dev/null | tr -d ' \r\n'); ALGO=size_only; DIG=""; ` +
		`if command -v sha256sum >/dev/null 2>&1; then ALGO=sha256; DIG=$(sha256sum ` + quoted + ` 2>/dev/null | awk '{print $1}'); ` +
		`elif command -v md5sum >/dev/null 2>&1; then ALGO=md5; DIG=$(md5sum ` + quoted + ` 2>/dev/null | awk '{print $1}'); fi; ` +
		`echo "size=${SIZE} algo=${ALGO} digest=${DIG}"; fi`
}

// TestGoldenAcliWriteCommandByteIdentical acli 写路径命令必须与收口前逐字一致。
func TestGoldenAcliWriteCommandByteIdentical(t *testing.T) {
	cases := []struct {
		name              string
		tmp, dest         string
		size              int64
		sha256Hex, md5Hex string
		mode              string
	}{
		{"常规", "/sf/data/local/a.acli.tmp.1", "/sf/data/local/a.acli", 20971520,
			strings.Repeat("ab", 32), strings.Repeat("cd", 16), "0755"},
		{"mode 缺省", "/sf/data/local/b.tmp", "/sf/data/local/b.acli", 1, "", "", ""},
		{"含单引号路径", "/sf/it's.tmp", "/sf/it's.acli", 7, strings.Repeat("0f", 32), strings.Repeat("1e", 16), "0644"},
		{"零字节", "/sf/z.tmp", "/sf/z.acli", 0, strings.Repeat("00", 32), strings.Repeat("00", 16), "0755"},
		{"含空格路径", "/sf/a b.tmp", "/sf/a b.acli", 42, strings.Repeat("9a", 32), strings.Repeat("8b", 16), "0755"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pkg := &acliPackage{Size: tc.size, SHA256: tc.sha256Hex, MD5: tc.md5Hex}
			got := buildAcliRemoteWriteCommand(tc.tmp, tc.dest, pkg, tc.mode)
			want := goldenAcliWriteCommand(tc.tmp, tc.dest, tc.size, tc.sha256Hex, tc.md5Hex, tc.mode)
			if got != want {
				t.Fatalf("acli 写命令字节发生变化（收口必须是抽取而非重写）:\n got %s\nwant %s", got, want)
			}
		})
	}
}

// TestGoldenAcliProbeCommandByteIdentical 探测命令同样逐字锁定。
func TestGoldenAcliProbeCommandByteIdentical(t *testing.T) {
	for _, path := range []string{
		"/sf/data/local/a.acli",
		"/sf/it's.acli",
		"/sf/a b.acli",
		"/sf/data/local/acli-installer_x86_64.acli",
	} {
		if got, want := buildAcliRemoteProbeCommand(path), goldenAcliProbeCommand(path); got != want {
			t.Fatalf("探测命令字节变化 path=%s:\n got %s\nwant %s", path, got, want)
		}
	}
}

// TestAcliSyncConsumerGoesThroughPrimitives S4a 验收：acli_sync 三段内部
// 实现必须真的走原语/骨架，而不是"原语建好了、链路还在自己手写并发/分轨"。
// 外壳（事件名/预算/retry_stage）不在本守卫范围：那是 S4b 的事。
func TestAcliSyncConsumerGoesThroughPrimitives(t *testing.T) {
	data, err := os.ReadFile("acli_package.go")
	if err != nil {
		t.Fatal(err)
	}
	src := string(data)
	if !strings.Contains(src, "remote.RunSplit(ctx, sshx.NewSSHRunner(s), outcome.Command)") {
		t.Error("installAcliPackage 必须经 remote.RunSplit 分轨执行（S4a）")
	}
	if !strings.Contains(src, "fanout.Each(ctx, targets,") {
		t.Error("verifyClusterPluginLanding 必须经 reconcile.Fanout.Each 编排（S4a）")
	}
	if strings.Contains(src, "sem := make(chan struct{}, spcFanoutConcurrency)") {
		t.Error("逐节点校验不得再手写 sem/wg 并发（唯一编排实现是 reconcile.Fanout）")
	}
	mainSrc, err := os.ReadFile("acli.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(mainSrc), "remote.Run(ctx, sshx.NewSSHRunner(s), \"uname -m\")") {
		t.Error("架构探测必须经 remote.Run 执行（S4a）")
	}
}
