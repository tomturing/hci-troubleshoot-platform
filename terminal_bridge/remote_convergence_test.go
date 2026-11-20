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
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/shared/remote"
	"testing"
)

// goldenShellQuote 是收口前 log_collect.go:187 的实现，逐字冻结。
func goldenShellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

// TestGoldenShellQuoteByteIdentical 转义形态逐字锁定（log_collect 侧）。
func TestGoldenShellQuoteByteIdentical(t *testing.T) {
	for _, v := range []string{"plain", "", "a'b", "$(x)", "/sf/a b", "a\"b", "`id`", "a;rm -rf /"} {
		if got, want := logcollect.ShellQuote(v), goldenShellQuote(v); got != want {
			t.Fatalf("shellQuote(%q) = %q, want %q", v, got, want)
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
		if got := remote.StripANSI(tc.in); got != tc.want {
			t.Fatalf("remote.StripANSI(%q) = %q, want %q", tc.in, got, tc.want)
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
