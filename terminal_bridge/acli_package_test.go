package main

// acli 安装包完整性链路的纯函数单测。
//
// 覆盖原则（第一性原理）：只测「判定逻辑」本身——哪些输入必须被判失败，因为
// 这里出现假通过，就等于把坏包放上生产节点，后面所有重试都只是重放错误。
// 凡是需要 SSH/HTTP 的路径都不在此测（它们是对远端事实的采集，不是判定）。

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// testAcliPackage 构造一个签名完整的假包，摘要长度与真实值一致（64/32 hex）。
func testAcliPackage() *acliPackage {
	return &acliPackage{
		Data:        []byte("fake-acli-binary"),
		Size:        1234,
		SHA256:      strings.Repeat("a1", 32),
		MD5:         strings.Repeat("b2", 16),
		Filename:    "acli-installer_v1.0.1_2026-10-29_10-00-00.acli",
		DownloadURL: "http://example.invalid/latest",
		RemoteBuild: "2026-10-29 10:00:00",
		DeclaredLen: 1234,
		FetchedAt:   time.Now(),
	}
}

func TestSanitizeAcliFilename(t *testing.T) {
	cases := []struct {
		name     string
		input    string
		arch     string
		expected string
	}{
		{"正常包名", "acli-installer_v1_2026-10-29_10-00-00.acli", "x86_64", "acli-installer_v1_2026-10-29_10-00-00.acli"},
		{"带目录前缀", "/var/tmp/acli-installer_v1.acli", "x86_64", "acli-installer_v1.acli"},
		{"路径穿越", "../../etc/passwd", "x86_64", "acli-installer_x86_64.acli"},
		{"穿越但以 acli 结尾", "../../evil.acli", "x86_64", "evil.acli"},
		{"纯穿越片段回退", "../..", "aarch64", "acli-installer_aarch64.acli"},
		{"非 acli 后缀回退", "installer.sh", "x86_64", "acli-installer_x86_64.acli"},
		{"空值回退", "", "x86_64", "acli-installer_x86_64.acli"},
		{"含空格回退", "acli installer.acli", "aarch64", "acli-installer_aarch64.acli"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := sanitizeAcliFilename(tc.input, tc.arch)
			if got != tc.expected {
				t.Fatalf("sanitizeAcliFilename(%q) = %q, want %q", tc.input, got, tc.expected)
			}
			if strings.Contains(got, "..") || strings.Contains(got, "/") {
				t.Fatalf("收敛结果仍含危险片段: %q", got)
			}
		})
	}
}

// TestBuildAcliRemoteWriteCommandOrdering 是本次修复的核心断言：
// 校验必须嵌在 mv **之前**。顺序一旦反了，坏包就已经在线上了。
func TestBuildAcliRemoteWriteCommandOrdering(t *testing.T) {
	pkg := testAcliPackage()
	cmd := buildAcliRemoteWriteCommand("/sf/data/local/a.acli.tmp.1", "/sf/data/local/a.acli", pkg, "0755")

	sizeIdx := strings.Index(cmd, `"$SIZE" != "1234"`)
	shaIdx := strings.Index(cmd, pkg.SHA256)
	md5Idx := strings.Index(cmd, pkg.MD5)
	mvIdx := strings.Index(cmd, "mv -f")

	if sizeIdx < 0 || shaIdx < 0 || md5Idx < 0 || mvIdx < 0 {
		t.Fatalf("命令缺少必需片段: size=%d sha=%d md5=%d mv=%d\n%s", sizeIdx, shaIdx, md5Idx, mvIdx, cmd)
	}
	if !(sizeIdx < mvIdx) {
		t.Fatalf("size 校验必须早于 mv: size=%d mv=%d", sizeIdx, mvIdx)
	}
	if !(shaIdx < mvIdx) {
		t.Fatalf("sha256 校验必须早于 mv: sha=%d mv=%d", shaIdx, mvIdx)
	}
	if !(md5Idx < mvIdx) {
		t.Fatalf("md5 校验必须早于 mv: md5=%d mv=%d", md5Idx, mvIdx)
	}
	// 失败分支必须删残包：否则下一次重试可能把半包当成有效包继续。
	if !strings.Contains(cmd, "rm -f '/sf/data/local/a.acli.tmp.1'") {
		t.Fatalf("失败分支必须清理临时文件:\n%s", cmd)
	}
	if !strings.Contains(cmd, acliWriteOKMark) || !strings.Contains(cmd, acliWriteFailMark) {
		t.Fatalf("命令必须回显成功/失败标记:\n%s", cmd)
	}
	if !strings.Contains(cmd, "chmod 0755") {
		t.Fatalf("命令必须设置可执行权限:\n%s", cmd)
	}
}

// TestBuildAcliRemoteWriteCommandQuoting 远端路径可能含单引号，未转义就是命令注入。
func TestBuildAcliRemoteWriteCommandQuoting(t *testing.T) {
	pkg := testAcliPackage()
	cmd := buildAcliRemoteWriteCommand("/sf/data/local/it's.tmp", "/sf/data/local/it's.acli", pkg, "")
	if strings.Contains(cmd, "it's") {
		t.Fatalf("单引号未转义，存在命令注入风险:\n%s", cmd)
	}
	if !strings.Contains(cmd, `it'\''s`) {
		t.Fatalf("单引号转义形态不符合 shellQuote 契约:\n%s", cmd)
	}
	if !strings.Contains(cmd, "chmod 0755") {
		t.Fatalf("mode 缺省应为 0755:\n%s", cmd)
	}
}

func TestParseAcliWriteOutput(t *testing.T) {
	sha := strings.Repeat("c3", 32)
	cases := []struct {
		name       string
		output     string
		wantSize   int64
		wantAlgo   string
		wantDigest string
		wantMoved  bool
		wantFail   string
	}{
		{
			name:       "成功",
			output:     "__ACLI_WRITE size=1234 algo=sha256 digest=" + sha + "\n__ACLI_WRITE_OK\n",
			wantSize:   1234,
			wantAlgo:   "sha256",
			wantDigest: sha,
			wantMoved:  true,
		},
		{
			name:     "大小不符",
			output:   "__ACLI_WRITE size=12 algo=sha256 digest=" + sha + "\n__ACLI_WRITE_FAIL size_mismatch\n",
			wantSize: 12, wantAlgo: "sha256", wantDigest: sha, wantFail: "size_mismatch",
		},
		{
			name:       "带登录 banner 噪声",
			output:     "Welcome to HCI\nLast login: xxx\n__ACLI_WRITE size=99 algo=size_only digest= \n__ACLI_WRITE_OK\n$ ",
			wantSize:   99,
			wantAlgo:   "size_only",
			wantDigest: "",
			wantMoved:  true,
		},
		{
			name:     "cat 失败",
			output:   "__ACLI_WRITE_FAIL cat_failed\n",
			wantFail: "cat_failed",
		},
		{
			name:   "空输出",
			output: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := parseAcliWriteOutput(tc.output)
			if got.RemoteSize != tc.wantSize || got.Algo != tc.wantAlgo || got.Digest != tc.wantDigest {
				t.Fatalf("解析元数据错误: %+v (want size=%d algo=%s digest=%s)",
					got, tc.wantSize, tc.wantAlgo, tc.wantDigest)
			}
			if got.Moved != tc.wantMoved {
				t.Fatalf("Moved=%v, want %v", got.Moved, tc.wantMoved)
			}
			if got.FailReason != tc.wantFail {
				t.Fatalf("FailReason=%q, want %q", got.FailReason, tc.wantFail)
			}
		})
	}
}

func TestVerifyAcliRemoteWrite(t *testing.T) {
	pkg := testAcliPackage()
	cases := []struct {
		name          string
		result        acliWriteResult
		wantIntegrity string
		wantCode      string
		wantErr       bool
	}{
		{
			name:          "sha256 一致",
			result:        acliWriteResult{Moved: true, RemoteSize: pkg.Size, Algo: "sha256", Digest: pkg.SHA256},
			wantIntegrity: "sha256",
		},
		{
			name:          "sha256 大小写不敏感",
			result:        acliWriteResult{Moved: true, RemoteSize: pkg.Size, Algo: "sha256", Digest: strings.ToUpper(pkg.SHA256)},
			wantIntegrity: "sha256",
		},
		{
			name:          "md5 一致",
			result:        acliWriteResult{Moved: true, RemoteSize: pkg.Size, Algo: "md5", Digest: pkg.MD5},
			wantIntegrity: "md5",
		},
		{
			name:          "只有大小可对比时降级但不阻断",
			result:        acliWriteResult{Moved: true, RemoteSize: pkg.Size, Algo: "size_only"},
			wantIntegrity: "size_only",
		},
		{
			name:     "sha256 不符",
			result:   acliWriteResult{Moved: true, RemoteSize: pkg.Size, Algo: "sha256", Digest: strings.Repeat("ff", 32)},
			wantCode: ErrInstallChecksum, wantErr: true,
		},
		{
			name:     "md5 不符",
			result:   acliWriteResult{Moved: true, RemoteSize: pkg.Size, Algo: "md5", Digest: strings.Repeat("ff", 16)},
			wantCode: ErrInstallChecksum, wantErr: true,
		},
		{
			name:     "落盘大小不符",
			result:   acliWriteResult{Moved: true, RemoteSize: pkg.Size - 1, Algo: "sha256", Digest: pkg.SHA256},
			wantCode: ErrInstallChecksum, wantErr: true,
		},
		{
			name:     "远端显式 size_mismatch",
			result:   acliWriteResult{FailReason: "size_mismatch"},
			wantCode: ErrInstallChecksum, wantErr: true,
		},
		{
			name:     "远端显式 digest_mismatch",
			result:   acliWriteResult{FailReason: "digest_mismatch"},
			wantCode: ErrInstallChecksum, wantErr: true,
		},
		{
			name:     "cat 失败归到上传阶段",
			result:   acliWriteResult{FailReason: "cat_failed"},
			wantCode: ErrInstallUpload, wantErr: true,
		},
		{
			name:     "mv 失败归到上传阶段",
			result:   acliWriteResult{FailReason: "move_failed"},
			wantCode: ErrInstallUpload, wantErr: true,
		},
		{
			name:     "没有任何结论也视为失败",
			result:   acliWriteResult{},
			wantCode: ErrInstallUpload, wantErr: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			integrity, err := verifyAcliRemoteWrite(pkg, tc.result)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("期望失败但通过了，integrity=%s", integrity)
				}
				var syncErr *acliSyncError
				if !errors.As(err, &syncErr) {
					t.Fatalf("错误未携带结构化信息: %v", err)
				}
				if syncErr.Code != tc.wantCode {
					t.Fatalf("错误码=%s, want %s", syncErr.Code, tc.wantCode)
				}
				if integrity != "" {
					t.Fatalf("失败时不应给出 integrity: %s", integrity)
				}
				return
			}
			if err != nil {
				t.Fatalf("期望通过但失败: %v", err)
			}
			if integrity != tc.wantIntegrity {
				t.Fatalf("integrity=%s, want %s", integrity, tc.wantIntegrity)
			}
		})
	}
}

func TestParseAcliProbeOutput(t *testing.T) {
	sha := strings.Repeat("d4", 32)
	cases := []struct {
		name string
		out  string
		size int64
		algo string
		dig  string
		miss bool
	}{
		{"包不存在", "__ACLI_PROBE_MISSING\n", 0, "", "", true},
		{"sha256 可用", "\x1b[32msize=1234 algo=sha256 digest=" + sha + "\x1b[0m\n", 1234, "sha256", sha, false},
		{"降级 size_only", "size=1234 algo=size_only digest= \n", 1234, "size_only", "", false},
		{"无任何输出", "", 0, "", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			size, algo, dig, miss := parseAcliProbeOutput(tc.out)
			if miss != tc.miss || size != tc.size || algo != tc.algo || dig != tc.dig {
				t.Fatalf("解析=(%d,%s,%s,%v), want (%d,%s,%s,%v)",
					size, algo, dig, miss, tc.size, tc.algo, tc.dig, tc.miss)
			}
		})
	}
}

// TestVerifyReusedRemotePackage 覆盖「只重试安装」时的复用门禁。
// 核心断言：没有本地基线就必须拒绝复用——那正是历史故障的形态（装了盘上的旧包）。
func TestVerifyReusedRemotePackage(t *testing.T) {
	pkg := testAcliPackage()
	cases := []struct {
		name    string
		pkg     *acliPackage
		size    int64
		algo    string
		digest  string
		wantErr bool
	}{
		{"无基线拒绝复用", nil, pkg.Size, "sha256", pkg.SHA256, true},
		{"大小一致 + sha256 一致", pkg, pkg.Size, "sha256", pkg.SHA256, false},
		{"大小一致 + md5 一致", pkg, pkg.Size, "md5", pkg.MD5, false},
		{"大小一致 + 仅大小可用", pkg, pkg.Size, "size_only", "", false},
		{"大小不符", pkg, pkg.Size + 1, "sha256", pkg.SHA256, true},
		{"sha256 不符", pkg, pkg.Size, "sha256", strings.Repeat("ff", 32), true},
		{"md5 不符", pkg, pkg.Size, "md5", strings.Repeat("ff", 16), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := verifyReusedRemotePackage(tc.pkg, tc.size, tc.algo, tc.digest)
			if tc.wantErr && err == nil {
				t.Fatal("期望拒绝复用但通过了")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("期望允许复用但被拒: %v", err)
			}
		})
	}
}

func TestBuildAcliInstallCommand(t *testing.T) {
	cmd := buildAcliInstallCommand("/sf/data/local/acli-installer_v1.acli")
	if !strings.HasPrefix(cmd, `export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin"; `) {
		t.Fatalf("安装命令必须前置 PATH（非交互 shell 无 /sf/bin）: %s", cmd)
	}
	if !strings.Contains(cmd, "'/sf/data/local/acli-installer_v1.acli' --install --force") {
		t.Fatalf("安装命令缺少 --install --force 或未做 shell 转义: %s", cmd)
	}
}

func TestAcliSyncErrorCode(t *testing.T) {
	cause := errors.New("boom")
	wrapped := newAcliSyncError(ErrInstallChecksum, "verify", false, "落盘校验不符", cause)

	code, detail, retryable := acliSyncErrorCode(wrapped, ErrInstallUpload)
	if code != ErrInstallChecksum {
		t.Fatalf("code=%s, want %s", code, ErrInstallChecksum)
	}
	if !strings.Contains(detail, "落盘校验不符") || !strings.Contains(detail, "boom") {
		t.Fatalf("detail 必须同时包含说明与根因: %s", detail)
	}
	if retryable {
		t.Fatal("retryable 应取自结构化错误本身（false）")
	}

	// 裸错误走 fallback，且必须默认可重试——否则前端连兜底按钮都没有。
	code, detail, retryable = acliSyncErrorCode(cause, ErrInstallUpload)
	if code != ErrInstallUpload || !retryable || detail != "boom" {
		t.Fatalf("fallback 行为不符: code=%s retryable=%v detail=%s", code, retryable, detail)
	}
}

func TestAcliPackageCache(t *testing.T) {
	cache := newAcliPackageCache()
	pkg := testAcliPackage()

	if got := cache.get("missing"); got != nil {
		t.Fatal("未命中应返回 nil")
	}

	cache.put("case@node", pkg)
	if got := cache.get("case@node"); got != pkg {
		t.Fatal("命中后应返回同一对象")
	}

	// 过期必须失效：官方 latest 可能已换包，长期缓存会让重试装回旧版本。
	cache.items["case@node"] = acliPackageCacheEntry{pkg: pkg, cachedAt: time.Now().Add(-acliPackageCacheTTL - time.Minute)}
	if got := cache.get("case@node"); got != nil {
		t.Fatal("过期条目应被淘汰")
	}

	cache.put("case@node", pkg)
	cache.drop("case@node")
	if got := cache.get("case@node"); got != nil {
		t.Fatal("drop 后不应命中")
	}

	// 空键与空包不得污染缓存。
	cache.put("", pkg)
	cache.put("k", nil)
	if got := cache.get(""); got != nil {
		t.Fatal("空键不应存缓存")
	}
}

func TestAcquireAcliTransfer(t *testing.T) {
	b := &Bridge{}
	release, ok := b.acquireAcliTransfer("case@1.1.1.1")
	if !ok {
		t.Fatal("首次抢占应成功")
	}

	// 同一会话重复抢占必须被拒：并发写同一 destPath 会互相覆盖。
	if _, second := b.acquireAcliTransfer("case@1.1.1.1"); second {
		t.Fatal("并发抢占应被拒绝")
	}

	// 不同会话互不影响。
	releaseOther, okOther := b.acquireAcliTransfer("case@2.2.2.2")
	if !okOther {
		t.Fatal("不同会话应可并行")
	}
	releaseOther()

	release()
	_, again := b.acquireAcliTransfer("case@1.1.1.1")
	if !again {
		t.Fatal("释放后应可再次抢占")
	}
}

func TestAcliOutputForLog(t *testing.T) {
	outcome := acliInstallOutcome{
		Command:  "acli --install --force",
		ExitCode: 2,
		Stdout:   strings.Repeat("progress\n", 100),
		Stderr:   "fatal: cannot distribute",
	}
	preview, stdoutBytes, stderrBytes := acliOutputForLog(outcome)
	if stdoutBytes != len(outcome.Stdout) || stderrBytes != len(outcome.Stderr) {
		t.Fatalf("字节数统计错误: %d/%d", stdoutBytes, stderrBytes)
	}
	if !strings.Contains(preview, "[stderr]") {
		t.Fatalf("预览必须显式标注 stderr 段落: %s", preview)
	}
	if !strings.Contains(preview, "fatal: cannot distribute") {
		t.Fatalf("预览必须保留 stderr 结论: %s", preview)
	}
}

func TestAcliProgressReader(t *testing.T) {
	data := make([]byte, acliUploadProgressStep*2+10)
	var fired []int64
	reader := newAcliProgressReader(data, acliUploadProgressStep, func(sent, total int64) {
		if total != int64(len(data)) {
			t.Fatalf("total=%d, want %d", total, len(data))
		}
		fired = append(fired, sent)
	})
	buf := make([]byte, 32*1024)
	total := 0
	for {
		n, err := reader.Read(buf)
		total += n
		if err != nil {
			break
		}
	}
	if total != len(data) {
		t.Fatalf("读取字节数=%d, want %d", total, len(data))
	}
	// 三步跨过两个上报点；末段不足一步不触发（由最终结果消息兜底）。
	if len(fired) != 2 {
		t.Fatalf("进度回调次数=%d, want 2 (%v)", len(fired), fired)
	}
	for i := 1; i < len(fired); i++ {
		if fired[i] <= fired[i-1] {
			t.Fatalf("进度必须单调递增: %v", fired)
		}
	}
}

func TestPkgFactsNilSafe(t *testing.T) {
	if pkgSize(nil) != 0 || pkgSHA(nil) != "" || pkgMD(nil) != "" {
		t.Fatal("nil 包必须返回零值，避免 install-only 复用路径 panic")
	}
	pkg := testAcliPackage()
	if pkgSize(pkg) != pkg.Size || pkgSHA(pkg) != pkg.SHA256 || pkgMD(pkg) != pkg.MD5 {
		t.Fatal("非 nil 包应返回真实事实")
	}
}
