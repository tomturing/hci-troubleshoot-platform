package remote

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"
)

// plainRunner 只实现 Runner：用于验证可选能力缺失时**显式降级**而不是静默假装。
type plainRunner struct {
	out  string
	code int
	err  error
}

func (p plainRunner) RunQuickCommand(context.Context, string) (string, int, error) {
	return p.out, p.code, p.err
}
func (p plainRunner) WriteStdin(context.Context, string, io.Reader) (string, int, error) {
	return p.out, p.code, p.err
}

// fakeRunner 实现全部三个接口，并按命令记录调用，供故障注入与清理断言。
type fakeRunner struct {
	quickFn  func(ctx context.Context, cmd string) (string, int, error)
	stdinFn  func(ctx context.Context, cmd string, r io.Reader) (string, int, error)
	streamFn func(ctx context.Context, cmd string) (io.ReadCloser, WaitFunc, error)
	calls    []string
}

func (f *fakeRunner) RunQuickCommand(ctx context.Context, cmd string) (string, int, error) {
	f.calls = append(f.calls, cmd)
	if f.quickFn != nil {
		return f.quickFn(ctx, cmd)
	}
	return "", 0, nil
}

func (f *fakeRunner) RunQuickCommandStreams(ctx context.Context, cmd string) (string, string, int, error) {
	f.calls = append(f.calls, cmd)
	if f.quickFn != nil {
		out, code, err := f.quickFn(ctx, cmd)
		return out, "", code, err
	}
	return "", "", 0, nil
}

func (f *fakeRunner) WriteStdin(ctx context.Context, cmd string, r io.Reader) (string, int, error) {
	f.calls = append(f.calls, cmd)
	if f.stdinFn != nil {
		return f.stdinFn(ctx, cmd, r)
	}
	return "", 0, nil
}

func (f *fakeRunner) ReadStream(ctx context.Context, cmd string) (io.ReadCloser, WaitFunc, error) {
	f.calls = append(f.calls, cmd)
	if f.streamFn != nil {
		return f.streamFn(ctx, cmd)
	}
	return io.NopCloser(strings.NewReader("")), func() error { return nil }, nil
}

// ── quote ────────────────────────────────────────────────────────────────────

func TestShellQuote(t *testing.T) {
	cases := []struct{ in, want string }{
		{"plain", "'plain'"},
		{"", "''"},
		{"a'b", `'a'\''b'`},
		{"base64", "'base64'"},
		{"$(reboot)", "'$(reboot)'"},
		{"a;rm -rf /", "'a;rm -rf /'"},
		{"`id`", "'`id`'"},
		{`a"b`, `'a"b'`},
		{"a b\tc", "'a b\tc'"},
	}
	for _, tc := range cases {
		if got := ShellQuote(tc.in); got != tc.want {
			t.Errorf("ShellQuote(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// shellUnquoteForTest 按 POSIX 单引号规则反向解析，并顺便检查"引号外不得出现
// shell 控制字符"。单测里跑不了真 shell，但这两条正是"不可能变成第二条命令"
// 的结构性前提：能原样还原输入 + 引号外无元字符。
func shellUnquoteForTest(s string) (string, error) {
	var b strings.Builder
	inQuote := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '\'' {
			inQuote = !inQuote
			continue
		}
		if !inQuote {
			if c == '\\' {
				i++
				if i >= len(s) {
					return "", errors.New("悬空转义")
				}
				b.WriteByte(s[i])
				continue
			}
			if strings.ContainsRune(";|&$<>` \t\n", rune(c)) {
				return "", errors.New("引号外出现 shell 控制字符: " + string(c))
			}
		}
		b.WriteByte(c)
	}
	if inQuote {
		return "", errors.New("单引号未闭合")
	}
	return b.String(), nil
}

// TestShellQuoteInjectionIsLiteral 注入串经转义后只能是字面量：
// 反向解析必须原样还原输入，且引号外不得出现任何 shell 控制字符。
func TestShellQuoteInjectionIsLiteral(t *testing.T) {
	for _, evil := range []string{
		"x'; reboot; echo '",
		"$(reboot)",
		"`id`",
		"a;rm -rf /",
		"a b|c",
		"it's",
		"",
	} {
		got := ShellQuote(evil)
		back, err := shellUnquoteForTest(got)
		if err != nil {
			t.Fatalf("ShellQuote(%q) = %q 不是安全字面量: %v", evil, got, err)
		}
		if back != evil {
			t.Fatalf("反向解析不等价: %q -> %q -> %q", evil, got, back)
		}
	}
}

// ── exec：S0 四种包装语义 ─────────────────────────────────────────────────────

func TestRunMinimalSemantics(t *testing.T) {
	r := plainRunner{out: "hello", code: 3, err: errors.New("boom")}
	res := Run(context.Background(), r, "cmd")
	if res.Stdout != "hello" || res.ExitCode != 3 || res.Err == nil {
		t.Fatalf("最小语义必须原样透传: %#v", res)
	}
	if res.SplitDegraded {
		t.Fatal("Run 不做分轨，不应标注降级")
	}
}

func TestRunSplitDegradedWhenUnsupported(t *testing.T) {
	res := RunSplit(context.Background(), plainRunner{out: "x"}, "cmd")
	if !res.SplitDegraded {
		t.Fatal("执行器不支持分轨时必须显式标注 SplitDegraded")
	}
	if res.Stdout != "x" {
		t.Fatalf("降级后仍应返回可得输出: %#v", res)
	}
}

func TestRunSplitSeparatesStreams(t *testing.T) {
	f := &fakeRunner{quickFn: func(context.Context, string) (string, int, error) {
		return "stat: not found", 127, errors.New("exit 127")
	}}
	res := RunSplit(context.Background(), f, "stat /x")
	if res.SplitDegraded {
		t.Fatal("支持分轨时不应标注降级")
	}
	if res.ExitCode != 127 {
		t.Fatalf("exit 127（命令不存在）必须可判: %#v", res)
	}
}

func TestRunCombinedMergesStderr(t *testing.T) {
	f := &fakeRunner{quickFn: func(context.Context, string) (string, int, error) {
		return "out", 0, nil
	}}
	// fakeRunner 的分轨实现把 stderr 置空，这里直接验证合并函数对空 stderr 的处理
	res := RunCombined(context.Background(), f, "cmd")
	if res.Stdout != "out" {
		t.Fatalf("stderr 为空时不应追加换行: %q", res.Stdout)
	}
}

func TestRunTruncatedKeepsFullBytes(t *testing.T) {
	long := strings.Repeat("a", 500)
	f := &fakeRunner{quickFn: func(context.Context, string) (string, int, error) { return long, 0, nil }}
	res := RunTruncated(context.Background(), f, "cmd", 100)
	if !res.Truncated {
		t.Fatal("超限必须标注 Truncated")
	}
	if res.Stdout != long[:100]+"..." {
		t.Fatalf("截断形态应为前 limit 字节 + ...: %q", res.Stdout)
	}
	if res.FullBytes != 500 {
		t.Fatalf("FullBytes 必须保留原始长度: %d", res.FullBytes)
	}
	// limit<=0 表示不截断
	res2 := RunTruncated(context.Background(), f, "cmd", 0)
	if res2.Truncated || res2.Stdout != long {
		t.Fatalf("limit<=0 不应截断: %#v", res2)
	}
}

func TestRunChunkedStopsAtFirstNonZero(t *testing.T) {
	codes := map[string]int{"a": 0, "b": 21, "c": 0}
	f := &fakeRunner{quickFn: func(_ context.Context, cmd string) (string, int, error) {
		return cmd, codes[cmd], nil
	}}
	out := RunChunked(context.Background(), f, []string{"a", "b", "c"})
	if out.Index != 1 {
		t.Fatalf("应在第 1 片停下: %d", out.Index)
	}
	if len(out.Results) != 2 {
		t.Fatalf("已执行片的结果必须全部保留（排障要看失败前写了什么）: %d", len(out.Results))
	}
	var exitErr *ExitCodeError
	if !errors.As(out.Err, &exitErr) || exitErr.ExitCode != 21 {
		t.Fatalf("非零退出必须可与通道错误区分: %#v", out.Err)
	}
	if len(f.calls) != 2 {
		t.Fatalf("停下后不得继续执行: %#v", f.calls)
	}
}

func TestRunChunkedAllSuccess(t *testing.T) {
	f := &fakeRunner{}
	out := RunChunked(context.Background(), f, []string{"a", "b"})
	if out.Err != nil || out.Index != 2 || len(out.Results) != 2 {
		t.Fatalf("全成功时 Index 应为片数: %#v", out)
	}
}

func TestRunChunkedRespectsContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	f := &fakeRunner{}
	out := RunChunked(ctx, f, []string{"a"})
	if out.Err == nil || len(f.calls) != 0 {
		t.Fatalf("ctx 已取消时不应执行任何片: %#v", out)
	}
}

// ── fs：写入协议 ─────────────────────────────────────────────────────────────

func TestBuildWriteCommandVerifyBeforeMove(t *testing.T) {
	spec := WriteSpec{Path: "/sf/data/local/a.acli", TmpPath: "/sf/data/local/a.acli.tmp.1",
		Size: 123, SHA256: strings.Repeat("ab", 32), MD5: strings.Repeat("cd", 16), Mode: "0755"}
	cmd := BuildWriteCommand(spec)
	// 结构性不变量：size/sha/md5 三道校验都必须出现在 mv 之前
	mvIdx := strings.Index(cmd, "mv -f")
	if mvIdx < 0 {
		t.Fatal("缺少 mv")
	}
	for _, token := range []string{"size_mismatch", "digest_mismatch", "sha256sum", "md5sum"} {
		idx := strings.Index(cmd, token)
		if idx < 0 || idx > mvIdx {
			t.Fatalf("%s 必须在 mv 之前（先校验再上线）: idx=%d mv=%d", token, idx, mvIdx)
		}
	}
	for _, token := range []string{"exit 20", "exit 21", "exit 22", "exit 23",
		WriteMark, WriteOKMark, WriteFailMark, "umask 077", "chmod 0755",
		"rm -f '/sf/data/local/a.acli.tmp.1'"} {
		if !strings.Contains(cmd, token) {
			t.Fatalf("缺少 %s:\n%s", token, cmd)
		}
	}
}

func TestBuildWriteCommandDefaultModeAndQuoting(t *testing.T) {
	cmd := BuildWriteCommand(WriteSpec{Path: "/sf/it's.acli", TmpPath: "/sf/it's.tmp", Size: 1})
	if !strings.Contains(cmd, "chmod 0755") {
		t.Fatalf("mode 缺省应为 0755:\n%s", cmd)
	}
	if !strings.Contains(cmd, `'/sf/it'\''s.acli'`) {
		t.Fatalf("含单引号路径必须按 POSIX 转义:\n%s", cmd)
	}
}

func TestParseWriteOutput(t *testing.T) {
	digest := strings.Repeat("ab", 32)
	cases := []struct {
		name       string
		output     string
		wantSize   int64
		wantAlgo   string
		wantMoved  bool
		wantReason string
	}{
		{"sha256", "banner\n" + WriteMark + "size=123 algo=sha256 digest=" + digest + "\n" + WriteOKMark, 123, "sha256", true, ""},
		{"md5 降级", WriteMark + "size=9 algo=md5 digest=" + strings.Repeat("cd", 16) + "\n" + WriteOKMark, 9, "md5", true, ""},
		{"size_only 降级", WriteMark + "size=9 algo=size_only digest=\n" + WriteOKMark, 9, "size_only", true, ""},
		{"size_mismatch", WriteMark + "size=1 algo=sha256 digest=x\n" + WriteFailMark + "size_mismatch", 1, "sha256", false, "size_mismatch"},
		{"cat_failed", WriteFailMark + "cat_failed", 0, "", false, "cat_failed"},
		{"空输出", "", 0, "", false, ""},
		{"ANSI 噪声", "\x1b[31m" + WriteMark + "size=5 algo=sha256 digest=ff\x1b[0m\n" + WriteOKMark, 5, "sha256", true, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseWriteOutput(tc.output)
			if got.RemoteSize != tc.wantSize || got.Algo != tc.wantAlgo ||
				got.Moved != tc.wantMoved || got.FailReason != tc.wantReason {
				t.Fatalf("解析不符: %#v", got)
			}
		})
	}
}

// TestParseWriteOutputAlgoCharsetContainsDigits 对抗性回归：algo 字符集写成
// [a-z_] 会让 sha256/md5 永远不匹配，"校验已通过"变成不可判定的空结果且不报错。
func TestParseWriteOutputAlgoCharsetContainsDigits(t *testing.T) {
	for _, algo := range []string{"sha256", "md5"} {
		got := ParseWriteOutput(WriteMark + "size=1 algo=" + algo + " digest=aa")
		if got.Algo != algo {
			t.Fatalf("algo=%s 必须可解析: %#v", algo, got)
		}
	}
}

func TestVerifyWriteIntegrityAndReasons(t *testing.T) {
	sha := strings.Repeat("ab", 32)
	md := strings.Repeat("cd", 16)
	spec := WriteSpec{Size: 100, SHA256: sha, MD5: md}
	cases := []struct {
		name          string
		result        WriteResult
		wantIntegrity string
		wantReason    string
	}{
		{"sha256 通过", WriteResult{Moved: true, RemoteSize: 100, Algo: "sha256", Digest: sha}, "sha256", ""},
		{"大写 digest 等价", WriteResult{Moved: true, RemoteSize: 100, Algo: "sha256", Digest: strings.ToUpper(sha)}, "sha256", ""},
		{"md5 通过", WriteResult{Moved: true, RemoteSize: 100, Algo: "md5", Digest: md}, "md5", ""},
		{"size_only 透传", WriteResult{Moved: true, RemoteSize: 100, Algo: "size_only"}, "size_only", ""},
		{"落盘 sha 不符", WriteResult{Moved: true, RemoteSize: 100, Algo: "sha256", Digest: strings.Repeat("ff", 32)}, "", "sha256_after_move"},
		{"落盘 md5 不符", WriteResult{Moved: true, RemoteSize: 100, Algo: "md5", Digest: strings.Repeat("ff", 16)}, "", "md5_after_move"},
		{"落盘大小不符", WriteResult{Moved: true, RemoteSize: 99, Algo: "sha256", Digest: sha}, "", "size_after_move"},
		{"上传大小不符", WriteResult{FailReason: "size_mismatch"}, "", "size_mismatch"},
		{"上传摘要不符", WriteResult{FailReason: "digest_mismatch"}, "", "digest_mismatch"},
		{"cat 失败", WriteResult{FailReason: "cat_failed"}, "", "cat_failed"},
		{"mv 失败", WriteResult{FailReason: "move_failed"}, "", "move_failed"},
		{"结论缺失", WriteResult{}, "", "remote_write_incomplete"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			integrity, err := VerifyWrite(spec, tc.result)
			if integrity != tc.wantIntegrity {
				t.Fatalf("integrity = %q, want %q", integrity, tc.wantIntegrity)
			}
			if tc.wantReason == "" {
				if err != nil {
					t.Fatalf("不应失败: %v", err)
				}
				return
			}
			var writeErr *WriteError
			if !errors.As(err, &writeErr) {
				t.Fatalf("失败必须是可判的 *WriteError: %#v", err)
			}
			if writeErr.Reason != tc.wantReason {
				t.Fatalf("reason = %q, want %q", writeErr.Reason, tc.wantReason)
			}
			if !writeErr.Retryable {
				t.Fatal("链路/校验类失败应可重试")
			}
		})
	}
}

// TestWriteFileFailureLeavesNoHalfPackage 故障注入：校验失败时正式路径不得被触碰。
// 断言方式是"命令里 mv 之前就有 exit"，加上远端回显未出现 OK 标记。
func TestWriteFileFailureLeavesNoHalfPackage(t *testing.T) {
	spec := WriteSpec{Path: "/sf/a.acli", Data: []byte("12345"), Size: 5,
		SHA256: strings.Repeat("ab", 32), MD5: strings.Repeat("cd", 16)}
	f := &fakeRunner{stdinFn: func(_ context.Context, cmd string, r io.Reader) (string, int, error) {
		// 远端事实：只收到 3 字节 → size_mismatch，脚本 exit 21，未 mv
		io.Copy(io.Discard, r)
		if !strings.Contains(cmd, "exit 21") {
			t.Errorf("命令必须含 size 不符的退出码分支")
		}
		return WriteMark + "size=3 algo=sha256 digest=xx\n" + WriteFailMark + "size_mismatch", 21, errors.New("exit status 21")
	}}
	result, err := WriteFile(context.Background(), f, spec)
	if err != nil {
		t.Fatalf("脚本以约定非零码终结时应把结论交给 VerifyWrite，而不是直接报通道错: %v", err)
	}
	if result.Moved {
		t.Fatal("校验失败绝不能标记为已 mv")
	}
	integrity, verifyErr := VerifyWrite(spec, result)
	if integrity != "" || verifyErr == nil {
		t.Fatalf("失败时 integrity 必须为空: %q %v", integrity, verifyErr)
	}
	var writeErr *WriteError
	if !errors.As(verifyErr, &writeErr) || writeErr.Reason != "size_mismatch" {
		t.Fatalf("reason 应为 size_mismatch: %#v", verifyErr)
	}
}

func TestWriteFileSuccessReportsIntegrity(t *testing.T) {
	data := []byte("hello")
	sha := "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
	spec := WriteSpec{Path: "/sf/a.acli", Data: data, Size: int64(len(data)), SHA256: sha}
	f := &fakeRunner{stdinFn: func(_ context.Context, _ string, r io.Reader) (string, int, error) {
		got, _ := io.ReadAll(r)
		if !bytes.Equal(got, data) {
			t.Errorf("stdin 字节必须与本地一致: %q", got)
		}
		return WriteMark + "size=5 algo=sha256 digest=" + sha + "\n" + WriteOKMark, 0, nil
	}}
	result, err := WriteFile(context.Background(), f, spec)
	if err != nil {
		t.Fatalf("不应失败: %v", err)
	}
	if result.Integrity != "sha256" || !result.Moved {
		t.Fatalf("成功必须透传校验档位: %#v", result)
	}
	if result.TmpPath == "" {
		t.Fatal("TmpPath 必须回填，供失败清理与日志复核")
	}
}

// TestWriteFileCancelCleansTmp ctx 取消时必须主动清理远端 tmp：
// 否则下次续传会把半包当成"已传部分"。
func TestWriteFileCancelCleansTmp(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	spec := WriteSpec{Path: "/sf/a.acli", TmpPath: "/sf/a.acli.tmp.9", Data: []byte("x"), Size: 1}
	f := &fakeRunner{stdinFn: func(context.Context, string, io.Reader) (string, int, error) {
		cancel()
		return "", -1, context.Canceled
	}}
	_, err := WriteFile(ctx, f, spec)
	var writeErr *WriteError
	if !errors.As(err, &writeErr) || writeErr.Reason != "timeout" {
		t.Fatalf("ctx 取消应归因为 timeout: %#v", err)
	}
	found := false
	for _, call := range f.calls {
		if strings.Contains(call, "rm -f '/sf/a.acli.tmp.9'") {
			found = true
		}
	}
	if !found {
		t.Fatalf("取消后必须清理 tmp: %#v", f.calls)
	}
}

func TestWriteFileChannelBrokenIsDistinct(t *testing.T) {
	spec := WriteSpec{Path: "/sf/a.acli", Data: []byte("x"), Size: 1}
	f := &fakeRunner{stdinFn: func(context.Context, string, io.Reader) (string, int, error) {
		return "", -1, errors.New("connection reset")
	}}
	_, err := WriteFile(context.Background(), f, spec)
	var writeErr *WriteError
	if !errors.As(err, &writeErr) || writeErr.Reason != "channel_broken" {
		t.Fatalf("通道异常必须与远端约定退出码区分: %#v", err)
	}
}

func TestWriteFileProgressReported(t *testing.T) {
	data := bytes.Repeat([]byte("a"), 5<<20)
	var reports []int64
	spec := WriteSpec{Path: "/sf/a", Data: data, Size: int64(len(data)),
		ProgressStep: 1 << 20, OnProgress: func(sent, total int64) { reports = append(reports, sent) }}
	f := &fakeRunner{stdinFn: func(_ context.Context, _ string, r io.Reader) (string, int, error) {
		io.Copy(io.Discard, r)
		return WriteMark + "size=5242880 algo=size_only digest=\n" + WriteOKMark, 0, nil
	}}
	if _, err := WriteFile(context.Background(), f, spec); err != nil {
		t.Fatalf("不应失败: %v", err)
	}
	if len(reports) == 0 {
		t.Fatal("5MiB / 1MiB 步进必须产生中间进度（否则长链路没有中间态）")
	}
	if reports[len(reports)-1] > int64(len(data)) {
		t.Fatalf("进度不得超过总量: %#v", reports)
	}
}

func TestCleanupTmpNilSafe(t *testing.T) {
	CleanupTmp(context.Background(), nil, "/x") // 不得 panic
	f := &fakeRunner{}
	CleanupTmp(context.Background(), f, "")
	if len(f.calls) != 0 {
		t.Fatalf("空路径不应发命令: %#v", f.calls)
	}
}

func TestStripANSI(t *testing.T) {
	if got := StripANSI("\x1b[31mFATA\x1b[0m 失败"); got != "FATA 失败" {
		t.Fatalf("ANSI 清洗不符: %q", got)
	}
	if got := StripANSI(""); got != "" {
		t.Fatalf("空串应原样返回: %q", got)
	}
}
