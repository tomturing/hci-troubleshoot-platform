package remote

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── probe ────────────────────────────────────────────────────────────────────

func TestBuildProbeCommandIsReadOnly(t *testing.T) {
	cmd := BuildProbeCommand("/sf/data/local/a.acli")
	for _, forbidden := range []string{"rm ", "mv ", "chmod ", ">", "mkdir"} {
		// `2>/dev/null` 是重定向到 null，不是写文件；只禁"写目标路径"的形态
		if forbidden == ">" {
			if strings.Contains(cmd, "> '/sf") || strings.Contains(cmd, "> \"/sf") {
				t.Fatalf("探测命令不得写目标路径:\n%s", cmd)
			}
			continue
		}
		if strings.Contains(cmd, forbidden) {
			t.Fatalf("探测必须只读，出现 %q:\n%s", forbidden, cmd)
		}
	}
	if !strings.Contains(cmd, "[ ! -s '/sf/data/local/a.acli' ]") {
		t.Fatalf("空文件与不存在必须同判为 missing:\n%s", cmd)
	}
	if !strings.Contains(cmd, ProbeMissingMark) {
		t.Fatal("缺少 missing 标记")
	}
	// 降级链必须完整：sha256sum → md5sum → size_only
	for _, token := range []string{"sha256sum", "md5sum", "ALGO=size_only"} {
		if !strings.Contains(cmd, token) {
			t.Fatalf("缺少降级档位 %s:\n%s", token, cmd)
		}
	}
}

func TestParseProbeOutput(t *testing.T) {
	sha := strings.Repeat("ab", 32)
	cases := []struct {
		name        string
		out         string
		wantSize    int64
		wantAlgo    string
		wantMissing bool
	}{
		{"missing", ProbeMissingMark, 0, "", true},
		{"空文件也判 missing", "banner\n" + ProbeMissingMark + "\n", 0, "", true},
		{"sha256", "size=123 algo=sha256 digest=" + sha, 123, "sha256", false},
		{"md5 降级", "size=9 algo=md5 digest=" + strings.Repeat("cd", 16), 9, "md5", false},
		{"size_only 降级", "size=9 algo=size_only digest=", 9, "size_only", false},
		{"大写 digest 归一", "size=1 algo=sha256 digest=" + strings.ToUpper(sha), 1, "sha256", false},
		{"无结论", "command not found", 0, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseProbeOutput(tc.out)
			if got.Size != tc.wantSize || got.Algo != tc.wantAlgo || got.Missing != tc.wantMissing {
				t.Fatalf("解析不符: %#v", got)
			}
			if tc.wantAlgo == "sha256" && got.Digest != sha {
				t.Fatalf("digest 应归一为小写: %q", got.Digest)
			}
		})
	}
}

// TestProbeFileErrorIsNotGuess 探测失败与探测到缺失是两种结论：
// 命令跑不起来时必须返回 error，不得猜"都存在"或"都不存在"。
func TestProbeFileErrorIsNotGuess(t *testing.T) {
	f := &fakeRunner{quickFn: func(context.Context, string) (string, int, error) {
		return "", -1, errors.New("channel closed")
	}}
	_, err := ProbeFile(context.Background(), f, "/sf/a")
	if err == nil {
		t.Fatal("命令跑不起来必须返回 error")
	}
}

func TestProbeFileMissingIsNotError(t *testing.T) {
	f := &fakeRunner{quickFn: func(context.Context, string) (string, int, error) {
		return ProbeMissingMark, 0, nil
	}}
	got, err := ProbeFile(context.Background(), f, "/sf/a")
	if err != nil {
		t.Fatalf("探测到缺失不是错误: %v", err)
	}
	if !got.Missing {
		t.Fatalf("必须如实标注缺失: %#v", got)
	}
}

func TestParseVersionOutput(t *testing.T) {
	out := "__ACLI_BIN__=/sf/bin/acli\n1.0.3\nbuild 2026-11-01T10:00:00Z\n"
	info := ParseVersionOutput(out)
	if info.Version != "1.0.3" || info.BuildTime != "2026-11-01T10:00:00Z" || info.Binary != "/sf/bin/acli" || info.Absent {
		t.Fatalf("版本解析不符: %#v", info)
	}
	absent := ParseVersionOutput("__ACLI_ABSENT__\n")
	if !absent.Absent || absent.Version != "" {
		t.Fatalf("未安装必须如实标注: %#v", absent)
	}
	twoPart := ParseVersionOutput("2.16\nbuild x\n")
	if twoPart.Version != "2.16" {
		t.Fatalf("两段版本号也应识别: %#v", twoPart)
	}
	// banner 噪声不得被当成版本号
	noise := ParseVersionOutput("Welcome to HCI\nLast login: 2026\n1.0.1\n")
	if noise.Version != "1.0.1" {
		t.Fatalf("应取首个形如 x.y[.z] 的行: %#v", noise)
	}
}

// ── transfer：续传三边界 ──────────────────────────────────────────────────────

func TestResolvePullStartBoundaries(t *testing.T) {
	cases := []struct {
		name                      string
		local, remote             int64
		wantOffset                int64
		wantDiscard, wantComplete bool
	}{
		{"首次下载", 0, 100, 0, false, false},
		{"正常续传", 40, 100, 40, false, false},
		{"恰好传完", 100, 100, 100, false, true},
		{"本地大于源包必丢弃", 120, 100, 0, true, false},
		{"远端大小未知按续传", 40, 0, 40, false, false},
		{"远端大小未知且本地为空", 0, -1, 0, false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ResolvePullStart(tc.local, tc.remote)
			if got.Offset != tc.wantOffset || got.Discard != tc.wantDiscard || got.Complete != tc.wantComplete {
				t.Fatalf("起点决策不符: %#v", got)
			}
		})
	}
}

func TestBuildPullCommand(t *testing.T) {
	if got := BuildPullCommand("/sf/a.zip", 0); got != "cat '/sf/a.zip'" {
		t.Fatalf("offset=0 必须用 cat（受限 shell 未必有 tail）: %q", got)
	}
	if got := BuildPullCommand("/sf/a.zip", -5); got != "cat '/sf/a.zip'" {
		t.Fatalf("负 offset 应按 0 处理: %q", got)
	}
	// tail -c +N 的 N 是 1-based：差一会重传首字节或漏一字节
	if got := BuildPullCommand("/sf/a.zip", 40); got != "tail -c +41 '/sf/a.zip'" {
		t.Fatalf("续传偏移必须 +1: %q", got)
	}
	if got := BuildPullCommand("/sf/it's.zip", 0); !strings.Contains(got, `'/sf/it'\''s.zip'`) {
		t.Fatalf("路径必须转义: %q", got)
	}
}

func TestProgressStepClamps(t *testing.T) {
	const minStep, maxStep int64 = 4 << 20, 64 << 20
	if got := ProgressStep(0); got != maxStep {
		t.Fatalf("总量未知应回退 maxStep: %d", got)
	}
	// 88 MB 的包：固定 64MiB 等于全程不上报（Q2026110300988）
	if got := ProgressStep(88 << 20); got != minStep {
		t.Fatalf("小包必须有足够粒度: %d", got)
	}
	if got := ProgressStep(20 << 30); got != maxStep {
		t.Fatalf("20GiB 不得产生上万次上报: %d", got)
	}
	mid := int64(1 << 30) // 1GiB → 1% ≈ 10MiB，落在区间内
	if got := ProgressStep(mid); got != mid/100 {
		t.Fatalf("区间内应取总量 1%%: %d", got)
	}
}

func TestHashFilePrefixStrict(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a.part")
	if err := os.WriteFile(path, []byte("hello world"), 0o600); err != nil {
		t.Fatal(err)
	}
	h := sha256.New()
	if err := HashFilePrefixStrict(path, 5, h); err != nil {
		t.Fatalf("前缀摘要不应失败: %v", err)
	}
	if hex.EncodeToString(h.Sum(nil)) != fmtSHA256("hello") {
		t.Fatal("前缀摘要必须只覆盖前 n 字节")
	}
	// 短读即残包失真，必须报错（看起来"有校验"实际永远对不上）
	if err := HashFilePrefixStrict(path, 999, sha256.New()); err == nil {
		t.Fatal("短读必须报错")
	}
	if err := HashFilePrefixStrict(path, 0, sha256.New()); err != nil {
		t.Fatalf("n<=0 应直接返回: %v", err)
	}
}

func fmtSHA256(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func TestPullRequiresStreamRunner(t *testing.T) {
	_, err := Pull(context.Background(), plainRunner{}, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: filepath.Join(t.TempDir(), "a.zip"), TotalSize: 10,
	})
	if err == nil || !strings.Contains(err.Error(), "ReadStream") {
		t.Fatalf("不支持流式拉取必须显式报错，不得退化成整包进内存: %v", err)
	}
}

func TestPullFresh(t *testing.T) {
	dir := t.TempDir()
	local := filepath.Join(dir, "sub", "a.zip")
	payload := strings.Repeat("z", 1000)
	var reports int
	f := &fakeRunner{streamFn: func(_ context.Context, cmd string) (io.ReadCloser, WaitFunc, error) {
		if cmd != "cat '/sf/a.zip'" {
			t.Errorf("首次拉取应用 cat: %q", cmd)
		}
		return io.NopCloser(strings.NewReader(payload)), func() error { return nil }, nil
	}}
	result, err := Pull(context.Background(), f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: local, TotalSize: int64(len(payload)),
		BlockSize: 128, OnProgress: func(written, total int64) { reports++ },
	})
	if err != nil {
		t.Fatalf("不应失败: %v", err)
	}
	if result.Written != int64(len(payload)) || result.SHA256 != fmtSHA256(payload) {
		t.Fatalf("边拉边摘要必须覆盖整包: %#v", result)
	}
	if reports == 0 {
		t.Fatal("必须上报进度")
	}
	got, _ := os.ReadFile(local)
	if string(got) != payload {
		t.Fatal("落地字节必须与源一致")
	}
	info, _ := os.Stat(local)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("本地副本权限应为 0600: %v", info.Mode().Perm())
	}
}

// TestPullCompleteSkipsNetwork 本地恰好等于源包时不得再读网络：
// 重跑交付能省掉整包重传。
func TestPullCompleteSkipsNetwork(t *testing.T) {
	dir := t.TempDir()
	local := filepath.Join(dir, "a.zip")
	payload := "complete-package"
	if err := os.WriteFile(local, []byte(payload), 0o600); err != nil {
		t.Fatal(err)
	}
	f := &fakeRunner{streamFn: func(context.Context, string) (io.ReadCloser, WaitFunc, error) {
		t.Error("已完整时不得启动远端读取")
		return nil, nil, errors.New("should not be called")
	}}
	result, err := Pull(context.Background(), f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: local, TotalSize: int64(len(payload)),
	})
	if err != nil || !result.Complete {
		t.Fatalf("应判定为已完成: %#v %v", result, err)
	}
	if result.SHA256 != fmtSHA256(payload) {
		t.Fatalf("已完成也必须给出整包摘要: %#v", result)
	}
	if len(f.calls) != 0 {
		t.Fatalf("不得发任何远端命令: %#v", f.calls)
	}
}

// TestPullResumeRehashesPrefix 续传时必须先把本地已有字节重新摘要一遍，
// 否则最终哈希只覆盖后半段——比不提供哈希更糟。
func TestPullResumeRehashesPrefix(t *testing.T) {
	dir := t.TempDir()
	local := filepath.Join(dir, "a.zip")
	prefix, rest := "AAAA", "BBBBBB"
	if err := os.WriteFile(local, []byte(prefix), 0o600); err != nil {
		t.Fatal(err)
	}
	f := &fakeRunner{streamFn: func(_ context.Context, cmd string) (io.ReadCloser, WaitFunc, error) {
		if cmd != "tail -c +5 '/sf/a.zip'" {
			t.Errorf("续传命令偏移必须 +1: %q", cmd)
		}
		return io.NopCloser(strings.NewReader(rest)), func() error { return nil }, nil
	}}
	result, err := Pull(context.Background(), f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: local, TotalSize: int64(len(prefix) + len(rest)),
	})
	if err != nil {
		t.Fatalf("不应失败: %v", err)
	}
	if result.Offset != 4 || result.Written != 10 {
		t.Fatalf("续传起点/总量不符: %#v", result)
	}
	if result.SHA256 != fmtSHA256(prefix+rest) {
		t.Fatalf("摘要必须覆盖整包（含本地前缀）: %#v", result)
	}
	got, _ := os.ReadFile(local)
	if string(got) != prefix+rest {
		t.Fatalf("续传不得重复写入前缀: %q", got)
	}
}

// TestPullDiscardsLargerLocal 本地大于源包必定不是本次的包：
// 同一份数据不可能是自己的前缀，追加进去会得到永远校验不过的坏包。
func TestPullDiscardsLargerLocal(t *testing.T) {
	dir := t.TempDir()
	local := filepath.Join(dir, "a.zip")
	if err := os.WriteFile(local, []byte("stale-longer-than-source"), 0o600); err != nil {
		t.Fatal(err)
	}
	payload := "new"
	f := &fakeRunner{streamFn: func(_ context.Context, cmd string) (io.ReadCloser, WaitFunc, error) {
		if !strings.HasPrefix(cmd, "cat ") {
			t.Errorf("丢弃后必须从头拉: %q", cmd)
		}
		return io.NopCloser(strings.NewReader(payload)), func() error { return nil }, nil
	}}
	result, err := Pull(context.Background(), f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: local, TotalSize: int64(len(payload)),
	})
	if err != nil || !result.Discarded {
		t.Fatalf("应判定残包不可信并丢弃: %#v %v", result, err)
	}
	got, _ := os.ReadFile(local)
	if string(got) != payload {
		t.Fatalf("丢弃后必须是新包: %q", got)
	}
}

// TestPullShortReadIsSizeMismatch 源包被截断/被 TMOUT 打断都会提前 EOF；
// 不在拉取层拦住，就会把短包当完整包继续上传。
func TestPullShortReadIsSizeMismatch(t *testing.T) {
	dir := t.TempDir()
	local := filepath.Join(dir, "a.zip")
	f := &fakeRunner{streamFn: func(context.Context, string) (io.ReadCloser, WaitFunc, error) {
		return io.NopCloser(strings.NewReader("short")), func() error { return nil }, nil
	}}
	result, err := Pull(context.Background(), f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: local, TotalSize: 100,
	})
	if !errors.Is(err, ErrPullSizeMismatch) {
		t.Fatalf("短读必须归因为字节数不符（可与网络中断区分）: %v", err)
	}
	if result.Written != 5 {
		t.Fatalf("失败时必须回填已拉字节（否则错误文案与账本自相矛盾）: %#v", result)
	}
	if result.SHA256 != "" {
		t.Fatal("短包不得给出摘要")
	}
}

func TestPullWaitErrorSurfaces(t *testing.T) {
	dir := t.TempDir()
	f := &fakeRunner{streamFn: func(context.Context, string) (io.ReadCloser, WaitFunc, error) {
		return io.NopCloser(strings.NewReader("abc")), func() error { return errors.New("exit status 1") }, nil
	}}
	_, err := Pull(context.Background(), f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: filepath.Join(dir, "a.zip"), TotalSize: 3,
	})
	if err == nil || !strings.Contains(err.Error(), "源包可能已被清理") {
		t.Fatalf("远端非零退出必须上抛（否则短包被当完整包）: %v", err)
	}
}

func TestPullCancelKeepsWritten(t *testing.T) {
	dir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	f := &fakeRunner{streamFn: func(context.Context, string) (io.ReadCloser, WaitFunc, error) {
		return io.NopCloser(&cancelReader{cancel: cancel, data: strings.Repeat("x", 4096)}),
			func() error { return nil }, nil
	}}
	result, err := Pull(ctx, f, PullSpec{
		RemotePath: "/sf/a.zip", LocalPath: filepath.Join(dir, "a.zip"), TotalSize: 1 << 20, BlockSize: 128,
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ctx 取消必须上抛: %v", err)
	}
	if result.Written <= 0 {
		t.Fatalf("取消时必须保留已拉字节供续传: %#v", result)
	}
}

// cancelReader 在读满一次后取消 ctx，模拟长链路中途被打断。
type cancelReader struct {
	cancel context.CancelFunc
	data   string
	pos    int
	fired  bool
}

func (c *cancelReader) Read(b []byte) (int, error) {
	if c.pos >= len(c.data) {
		return 0, io.EOF
	}
	n := copy(b, c.data[c.pos:])
	c.pos += n
	if !c.fired {
		c.fired = true
		c.cancel()
	}
	return n, nil
}

// ── marker：双协议 ───────────────────────────────────────────────────────────

func TestMarkerFormatAndParse(t *testing.T) {
	line := Format("config", StatusOK, [2]string{"sha", "abc123"}, [2]string{"path", "/sf/a b.json"})
	if !strings.HasPrefix(line, "SYNC_CONFIG_OK ") {
		t.Fatalf("新协议形态不符: %q", line)
	}
	m := Parse("config", "banner\n"+line+"\n")
	if m.Kind != "config" || m.Status != StatusOK || m.Degraded {
		t.Fatalf("新协议解析不应降级: %#v", m)
	}
	if m.Field("sha") != "abc123" {
		t.Fatalf("字段解析不符: %#v", m.Fields)
	}
	if m.Field("path") != "/sf/a b.json" {
		t.Fatalf("含空格的值必须完整还原: %#v", m.Fields)
	}
}

func TestMarkerParsePrefersNewOverLegacy(t *testing.T) {
	out := WriteOKMark + "\n" + Format("acli", StatusError, [2]string{"reason", "digest_mismatch"})
	m := Parse("acli", out)
	if m.Degraded || m.Status != StatusError {
		t.Fatalf("必须优先读新格式（取最后一条终态）: %#v", m)
	}
	if m.Field("reason") != "digest_mismatch" {
		t.Fatalf("reason 字段不符: %#v", m.Fields)
	}
}

func TestMarkerParseLegacyDegraded(t *testing.T) {
	cases := []struct {
		kind, out, wantStatus, wantReason string
	}{
		{"acli", WriteOKMark, StatusOK, ""},
		{"acli", WriteFailMark + "size_mismatch", StatusError, "size_mismatch"},
		{"acli", ProbeMissingMark, StatusSkipped, ""},
		{"spc", "SPC_WRITE_OK orig=abc", StatusOK, ""},
		{"spc", "SPC_RESTORE_SKIP reason=no_backup", StatusSkipped, "no_backup"},
		{"spc", "SPC_PREPARE_FAIL reason=backup_failed", StatusError, "backup_failed"},
		{"plugin", "ACP_ERROR=mkdir_failed", StatusError, "mkdir_failed"},
	}
	for _, tc := range cases {
		t.Run(tc.kind+"/"+tc.wantStatus, func(t *testing.T) {
			m := Parse(tc.kind, tc.out)
			if !m.Degraded {
				t.Fatalf("旧格式必须显式标注降级: %#v", m)
			}
			if m.Status != tc.wantStatus {
				t.Fatalf("status = %q, want %q", m.Status, tc.wantStatus)
			}
			if tc.wantReason != "" && m.Field("reason") != tc.wantReason {
				t.Fatalf("reason = %q, want %q", m.Field("reason"), tc.wantReason)
			}
		})
	}
}

// TestMarkerParseNoConclusion 没有结论时返回零值：调用方必须据此判"结论缺失"，
// 不得把"没读到标记"当成成功。
func TestMarkerParseNoConclusion(t *testing.T) {
	for _, out := range []string{"", "command not found", "some random output", "SYNC_CONFIG_UNKNOWN"} {
		m := Parse("config", out)
		if m.Kind != "" || m.Status != "" {
			t.Fatalf("无结论必须返回零值 Marker: %#v (out=%q)", m, out)
		}
	}
}

// TestMarkerRoundTripWithQuotes 回归：Format 对含空格/单引号的值加引号，
// Parse 必须能原样还原。早期用 strings.Fields 切分会把引号内空格切开，
// 字段静默丢一半且看起来"解析成功了"；用 strings.Trim 剥引号则会留下 `\'` 残渣。
func TestMarkerRoundTripWithQuotes(t *testing.T) {
	values := []string{
		"/sf/a b.json",
		"it's a path",
		"a'b c'd",
		"plain",
		"",
		"tab\tinside",
	}
	for _, v := range values {
		line := Format("spc", StatusOK, [2]string{"path", v}, [2]string{"tail", "end"})
		m := Parse("spc", line)
		if m.Status != StatusOK {
			t.Fatalf("值 %q 使状态解析失败: %q -> %#v", v, line, m)
		}
		if got := m.Field("path"); got != v {
			t.Fatalf("值往返不等价: %q -> %q -> %q", v, line, got)
		}
		// 后续字段不得被前一个字段的引号吞掉
		if m.Field("tail") != "end" {
			t.Fatalf("后续字段丢失: %q -> %#v", line, m.Fields)
		}
		if got := Field(line, "path"); got != v {
			t.Fatalf("Field 与 Parse 口径必须一致: %q != %q", got, v)
		}
	}
}

func TestMarkerHasAndField(t *testing.T) {
	out := "banner\nSPC_PREPARE_OK orig=abc123 reason=\n"
	if !Has(out, "SPC_PREPARE_OK") {
		t.Fatal("Has 应命中独立回显行")
	}
	if Has(out, "SPC_WRITE_OK") {
		t.Fatal("Has 不得误命中")
	}
	if got := Field(out, "orig"); got != "abc123" {
		t.Fatalf("Field 取值不符: %q", got)
	}
	// 只认独立成词的 key：noreason=y 不应被 reason 命中
	if got := Field("noreason=y", "reason"); got != "" {
		t.Fatalf("Field 不得子串误命中: %q", got)
	}
	// ANSI 噪声不得影响判定
	if !Has("\x1b[32mSPC_WRITE_OK\x1b[0m", "SPC_WRITE_OK") {
		t.Fatal("Has 应先清洗 ANSI")
	}
}
