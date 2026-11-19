package main

// remote_convergence_test.go —— S1 收口的两类守卫。
//
// 一、golden 逐字对比（设计 §八#13 验收分轨）：
//
//	acli 写路径的命令字节被**逐字锁定**。收口是"抽取+委托"，不是重写，
//	因此远端行为必须一个字符都不变。基准实现冻结在本文件里（golden* 函数），
//	与生产代码完全独立：remote 侧任何改动都会让对比失败，而不是让基准跟着漂。
//
//	config/spc 走行为级验收（tmp+mv 必然改字节），不在本文件锁定。
//
// 二、分层静态守卫（设计 §七硬约束）：
//
//	remote 禁止 import main 与 reconcile。这条约束靠 review 守不住——
//	一次"顺手复用"就能把横向原语变成认识具体产品的纵向逻辑，因此用测试钉死。

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// goldenShellQuote 是收口前 log_collect.go:187 的实现，逐字冻结。
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

// TestGoldenShellQuoteByteIdentical 转义形态逐字锁定（log_collect 侧）。
func TestGoldenShellQuoteByteIdentical(t *testing.T) {
	for _, v := range []string{"plain", "", "a'b", "$(x)", "/sf/a b", "a\"b", "`id`", "a;rm -rf /"} {
		if got, want := shellQuote(v), goldenShellQuote(v); got != want {
			t.Fatalf("shellQuote(%q) = %q, want %q", v, got, want)
		}
	}
}

// TestGoldenPullCommandByteIdentical 拉取命令逐字锁定：offset=0 用 cat、
// 续传用 tail -c +offset+1（1-based，差一会重传首字节或漏一字节）。
func TestGoldenPullCommandByteIdentical(t *testing.T) {
	cases := []struct {
		path   string
		offset int64
		want   string
	}{
		{"/sf/a.zip", 0, "cat '/sf/a.zip'"},
		{"/sf/a.zip", -1, "cat '/sf/a.zip'"},
		{"/sf/a.zip", 40, "tail -c +41 '/sf/a.zip'"},
		{"/sf/it's.zip", 1, "tail -c +2 '/sf/it'\\''s.zip'"},
	}
	for _, tc := range cases {
		if got := buildPullCommand(tc.path, tc.offset); got != tc.want {
			t.Fatalf("buildPullCommand(%q,%d) = %q, want %q", tc.path, tc.offset, got, tc.want)
		}
	}
}

// TestGoldenStripAnsiBehaviour ANSI 清洗正则与收口前一致（含中文与多段转义）。
func TestGoldenStripAnsiBehaviour(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", ""},
		{"\x1b[31mFATA\x1b[0m 收集失败", "FATA 收集失败"},
		{"\x1b[1;32mOK\x1b[0m", "OK"},
		{"无转义", "无转义"},
		{"\x1b[31m状态：\x1b[0m异常", "状态：异常"},
	}
	for _, tc := range cases {
		if got := stripAnsi(tc.in); got != tc.want {
			t.Fatalf("stripAnsi(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// TestRemotePackageHasNoUpwardImport 分层硬约束：remote 禁止 import main 与 reconcile。
//
// 为什么必须静态钉死：一旦 remote 认识了具体会话类型或期望值语义，
// 四条链路就再也无法共用同一份写入/探测/拉取实现——收口会在下一次
// "顺手复用"里静默失效，而编译与功能测试都不会报错。
func TestRemotePackageHasNoUpwardImport(t *testing.T) {
	dir := filepath.Join("internal", "shared", "remote")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("remote 包目录不存在（S1 未落地？）: %v", err)
	}
	fset := token.NewFileSet()
	checked := 0
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") {
			continue
		}
		path := filepath.Join(dir, name)
		file, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("解析 %s 失败: %v", path, err)
		}
		checked++
		for _, imp := range file.Imports {
			value, err := strconv.Unquote(imp.Path.Value)
			if err != nil {
				t.Fatalf("%s 的 import 路径不可解析: %v", path, err)
			}
			if value == "terminal_bridge" {
				t.Errorf("%s 禁止 import main 包（横向原语不得认识具体会话类型）", path)
			}
			if strings.Contains(value, "/reconcile") {
				t.Errorf("%s 禁止 import reconcile（依赖方向只能是 reconcile → remote）", path)
			}
			if strings.HasPrefix(value, "terminal_bridge/") && !strings.HasPrefix(value, "terminal_bridge/internal/shared/") {
				t.Errorf("%s 禁止 import 链路侧包 %s", path, value)
			}
		}
	}
	if checked == 0 {
		t.Fatal("没有扫到任何 remote 源文件，守卫形同虚设")
	}
}

// TestReconcileOnlyImportsRemote 反向守卫：reconcile 只能依赖 remote 与标准库。
// S2 已落地，目录必须存在且扫到源文件——否则守卫形同虚设。
func TestReconcileOnlyImportsRemote(t *testing.T) {
	dir := filepath.Join("internal", "shared", "reconcile")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("reconcile 包必须已落地（S2）：%v", err)
	}
	fset := token.NewFileSet()
	checked := 0
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		path := filepath.Join(dir, name)
		file, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("解析 %s 失败: %v", path, err)
		}
		checked++
		for _, imp := range file.Imports {
			value, err := strconv.Unquote(imp.Path.Value)
			if err != nil {
				t.Fatal(err)
			}
			if value == "terminal_bridge" {
				t.Errorf("%s 禁止 import main 包（骨架不得认识 WS 外壳）", path)
			}
			if strings.HasPrefix(value, "terminal_bridge/") &&
				value != "terminal_bridge/internal/shared/remote" &&
				!strings.HasPrefix(value, "terminal_bridge/internal/shared/reconcile") {
				t.Errorf("%s 只能依赖 remote 与标准库，出现 %s", path, value)
			}
		}
	}
	if checked == 0 {
		t.Fatal("没有扫到任何 reconcile 源文件，守卫形同虚设")
	}
}

// TestReconcileMetadataNeverDrivesBranch 补丁 5 的静态守卫：
// Metadata 只能是展示/trace，骨架内不得出现 Metadata[...] 形式的读写。
// 逃生舱一经滥用（if md["force"] 之类），收口目标就失效了；
// 需要新语义时扩强类型字段（如 FileSpec.Dir），而不是塞进 Metadata。
//
// 走 AST 而非文本扫描：文本会把"禁止 Metadata[...] 读取"这条注释本身判成违规，
// 而 AST 里注释根本不存在，同时能抓住任何真实的索引式读写（含别名赋值）。
func TestReconcileMetadataNeverDrivesBranch(t *testing.T) {
	dir := filepath.Join("internal", "shared", "reconcile")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	checked := 0
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		path := filepath.Join(dir, name)
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatalf("解析 %s 失败: %v", path, err)
		}
		checked++
		ast.Inspect(file, func(node ast.Node) bool {
			index, ok := node.(*ast.IndexExpr)
			if !ok {
				return true
			}
			selector, ok := index.X.(*ast.SelectorExpr)
			if ok && selector.Sel.Name == "Metadata" {
				t.Errorf("%s:%d 出现 Metadata[...] 读写：Metadata 不得驱动流程分支（补丁 5）",
					name, fset.Position(index.Pos()).Line)
			}
			return true
		})
	}
	if checked == 0 {
		t.Fatal("没有扫到任何 reconcile 源文件")
	}
}

// TestConfigConsumerGoesThroughReconcile S2 验收：ConfigFile 必须真的走骨架，
// 而不是"骨架建好了、消费者还在自己拼 printf"。后者会让收口只完成一半。
func TestConfigConsumerGoesThroughReconcile(t *testing.T) {
	data, err := os.ReadFile("log_collect_config.go")
	if err != nil {
		t.Fatal(err)
	}
	src := string(data)
	if !strings.Contains(src, "reconcile.Run(") {
		t.Error("writeLogCollectConfig 必须经 reconcile.Run 执行（S2 消费者迁移）")
	}
	if strings.Contains(src, "printf '%%s' %s > %s") {
		t.Error("config 写入段不得再自己拼 printf 直写（唯一写入实现是 remote.WriteFile）")
	}
}

// TestUploadStreamDeadCodeRemoved 死代码必须真的删掉：保留一份"看起来能用
// 但没有校验"的写入函数，等于给未来的人留一个绕过完整性协议的入口。
func TestUploadStreamDeadCodeRemoved(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		data, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "func (s *sshx.SSHSession) uploadStream(") {
			t.Errorf("%s 仍定义无校验的 uploadStream（应删除，唯一写入实现是 remote.WriteFile）", name)
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
