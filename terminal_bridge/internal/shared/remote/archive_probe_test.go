package remote

import (
	"context"
	"strings"
	"testing"
)

// 本组用例锁死 Q2026102900165（2026-10-29）的回归边界：
// aSV 受限 shell 里 stat 不存在（exit 127），包却真在盘上，ls -l 可用。
// 旧实现把这条路径判成 exists=false，平台因此对着盘上的包断言「已被插件删除」。
func TestDecideArchiveProbe(t *testing.T) {
	cases := []struct {
		name    string
		stat    archiveProbeAttempt
		ls      archiveProbeAttempt
		wantLvl string
		wantEx  *bool
		wantSz  int64
		// wantDiag 为空表示不检查；非空表示必须包含这些片段。
		wantDiag []string
		// wantUnavail 为真表示必须把「无法确认」归因为节点缺命令（LC-ENV-009），
		// 而不是通道异常（LC-TRANSFER-006）——两者处理动作完全不同。
		wantUnavail bool
	}{
		{
			name:     "stat 正常时直接用 stat 的大小",
			stat:     archiveProbeAttempt{Stdout: "29900423\n"},
			ls:       archiveProbeAttempt{Stdout: "-rw-r--r-- 1 root root 29900423 Oct 29 10:51 /sf/x.zip"},
			wantLvl:  "stat",
			wantEx:   boolPtr(true),
			wantSz:   29900423,
			wantDiag: nil,
		},
		{
			name:     "stat 命令不存在时走 ls 兜底（本单现场）",
			stat:     archiveProbeAttempt{Stderr: "stat: not found", Code: 127, Err: errExit(127)},
			ls:       archiveProbeAttempt{Stdout: "-rw-r--r-- 1 root root 29900423 Oct 29 10:51 /sf/x.zip"},
			wantLvl:  "ls",
			wantEx:   boolPtr(true),
			wantSz:   29900423,
			wantDiag: []string{"stat:exit=127", "stat: not found"},
		},
		{
			name:     "stat 成功但大小解析不出：存在性是确定的",
			stat:     archiveProbeAttempt{Stdout: "not-a-number"},
			ls:       archiveProbeAttempt{Stderr: "ls: not found", Code: 127, Err: errExit(127)},
			wantLvl:  "stat",
			wantEx:   boolPtr(true),
			wantSz:   0,
			wantDiag: []string{"ls:exit=127"},
		},
		{
			name:     "0 字节包同样算存在（不能因 size==0 判不存在）",
			stat:     archiveProbeAttempt{Stdout: "0"},
			ls:       archiveProbeAttempt{Stderr: "ls: not found", Code: 127, Err: errExit(127)},
			wantLvl:  "stat",
			wantEx:   boolPtr(true),
			wantSz:   0,
			wantDiag: []string{"ls:exit=127"},
		},
		{
			name:     "两级都明确说文件不存在才允许判 false",
			stat:     archiveProbeAttempt{Stderr: "stat: cannot stat '/sf/x.zip': No such file or directory", Code: 1, Err: errExit(1)},
			ls:       archiveProbeAttempt{Stderr: "ls: /sf/x.zip: No such file or directory", Code: 2, Err: errExit(2)},
			wantLvl:  "missing",
			wantEx:   boolPtr(false),
			wantSz:   0,
			wantDiag: []string{"stat:exit=1", "ls:exit=2"},
		},
		{
			name:     "两级命令都不可用：无法确认，不得判 missing",
			stat:     archiveProbeAttempt{Stderr: "stat: not found", Code: 127, Err: errExit(127)},
			ls:       archiveProbeAttempt{Stderr: "ls: not found", Code: 127, Err: errExit(127)},
			wantLvl:  "none",
			wantEx:   nil,
			wantSz:   0,
			wantDiag: []string{"stat:exit=127", "ls:exit=127"},
			// 两级都是 127：这是节点缺命令，归 LC-ENV-009。
			wantUnavail: true,
		},
		{
			// 极端情况：命令不存在但输出里恰好带 "not exist" 字样（如受限 shell 的
			// "command not exist"）。unavailable 必须压过 saidMissing，否则又回到旧缺陷。
			name:     "命令不可用的那级即使文本像 missing 也不算证据",
			stat:     archiveProbeAttempt{Stderr: "sh: stat: command not exist", Code: 127, Err: errExit(127)},
			ls:       archiveProbeAttempt{Stderr: "ls: not found", Code: 127, Err: errExit(127)},
			wantLvl:  "none",
			wantEx:   nil,
			wantSz:   0,
			wantDiag: nil,
			// 两级都是命令不可用：这是节点环境缺命令，不是通道问题。
			wantUnavail: true,
		},
		{
			name:     "一级命令缺失、一级超时仍判通道异常",
			stat:     archiveProbeAttempt{Stderr: "stat: not found", Code: 127, Err: errExit(127)},
			ls:       archiveProbeAttempt{Err: context.DeadlineExceeded},
			wantLvl:  "none",
			wantEx:   nil,
			wantSz:   0,
			wantDiag: []string{"stat:exit=127"},
			// 超时不是「命令不存在」的证据，不能算到节点环境头上。
			wantUnavail: false,
		},
		{
			name:        "权限拒绝不算命令缺失（要客户改权限，不是放开白名单）",
			stat:        archiveProbeAttempt{Stderr: "stat: cannot stat '/sf': Permission denied", Code: 1, Err: errExit(1)},
			ls:          archiveProbeAttempt{Stderr: "ls: cannot open directory: Permission denied", Code: 2, Err: errExit(2)},
			wantLvl:     "none",
			wantEx:      nil,
			wantSz:      0,
			wantDiag:    []string{"Permission denied"},
			wantUnavail: false,
		},
		{
			name:     "ls 成功但输出解析不出：存在确定、大小未知",
			stat:     archiveProbeAttempt{Stderr: "stat: not found", Code: 127, Err: errExit(127)},
			ls:       archiveProbeAttempt{Stdout: "total 0"},
			wantLvl:  "ls",
			wantEx:   boolPtr(true),
			wantSz:   0,
			wantDiag: []string{"stat:exit=127"},
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := decideArchiveProbe(c.stat, c.ls)
			if got.Level != c.wantLvl {
				t.Fatalf("Level = %q, want %q", got.Level, c.wantLvl)
			}
			if (got.Exists == nil) != (c.wantEx == nil) {
				t.Fatalf("Exists 三态不一致: got=%v want=%v", fmtPtr(got.Exists), fmtPtr(c.wantEx))
			}
			if got.Exists != nil && *got.Exists != *c.wantEx {
				t.Fatalf("Exists = %v, want %v", *got.Exists, *c.wantEx)
			}
			if got.Size != c.wantSz {
				t.Fatalf("Size = %d, want %d", got.Size, c.wantSz)
			}
			if got.Unavailable != c.wantUnavail {
				t.Fatalf("Unavailable = %v, want %v（决定归 LC-ENV-009 还是 LC-TRANSFER-006）", got.Unavailable, c.wantUnavail)
			}
			for _, frag := range c.wantDiag {
				if !strings.Contains(got.Diag, frag) {
					t.Fatalf("Diag 缺少 %q，实际为 %q", frag, got.Diag)
				}
			}
			// 反向断言：明确不存在的结论绝不允许由「命令不可用」单独推出。
			if got.Exists != nil && !*got.Exists && got.Level != "missing" {
				t.Fatalf("判不存在必须来自 missing 级，实际 Level=%q", got.Level)
			}
		})
	}
}

// TestProbeLogCollectArchiveNoSession 锁死“探测不了”的出口：返回三态 nil 而不是 false。
func TestProbeLogCollectArchiveNoSession(t *testing.T) {
	for _, rawPath := range []string{"/sf/x.zip", ""} {
		got := ProbeArchive(context.Background(), nil, rawPath)
		if got.Exists != nil {
			t.Fatalf("无会话时不得给出存在性结论: %v", *got.Exists)
		}
		if got.Level != "none" || got.Diag == "" {
			t.Fatalf("无会话时必须留下诊断: level=%q diag=%q", got.Level, got.Diag)
		}
	}
}

func TestArchiveProbeAttemptUnavailable(t *testing.T) {
	cases := []struct {
		in   archiveProbeAttempt
		want bool
	}{
		{archiveProbeAttempt{Code: 127}, true},
		{archiveProbeAttempt{Code: 126}, true},
		{archiveProbeAttempt{Stderr: "stat: not found", Code: 127}, true},
		{archiveProbeAttempt{Stderr: "unknown command: stat", Code: 1}, true},
		{archiveProbeAttempt{Stderr: "stat: invalid option -- 'c'", Code: 1}, true},
		{archiveProbeAttempt{Stderr: "ls: /sf/x.zip: No such file or directory", Code: 2}, false},
		{archiveProbeAttempt{Stdout: "1024"}, false},
	}
	for _, c := range cases {
		if got := c.in.unavailable(); got != c.want {
			t.Fatalf("%+v unavailable() = %v, want %v", c.in, got, c.want)
		}
	}
}

func boolPtr(v bool) *bool { return &v }

func fmtPtr(p *bool) string {
	if p == nil {
		return "nil"
	}
	if *p {
		return "true"
	}
	return "false"
}

type probeTestExit int

func (e probeTestExit) Error() string { return "exit" }

func errExit(code int) error { return probeTestExit(code) }

// TestParseLsFieldSize 覆盖 stat 失败后的兜底解析。
//
// 背景：staging 实测显示 stat 在所有采集窗口里都没成功过，
// 所以兜底路径必须是「宁可返回 false，也不能返回错误的数字」——
// 一旦把别的字段（权限、时间、目录里的另一个文件）当成大小，用户看到的就是假数据。
func TestParseLsFieldSize(t *testing.T) {
	cases := []struct {
		name   string
		output string
		want   int64
		ok     bool
	}{
		{"普通文件", "-rw-r--r-- 1 root root 314572800 Oct 27 10:36 /sf/data/local/acli/tmp/data/log_collect/Q1.zip", 314572800, true},
		{"文件名含空格", "-rw-r--r-- 1 root root 314572800 Oct 27 10:36 /sf/data/local/acli/tmp/data/log_collect/Q 1.zip", 314572800, true},
		{"目录统计行", "total 0", 0, false},
		{"空输出", "", 0, false},
		{"零字节", "-rw-r--r-- 1 root root 0 Oct 27 10:36 /x.zip", 0, false},
		{"非数字字段", "-rw-r--r-- 1 root root abc Oct 27 10:36 /x.zip", 0, false},
		{"通配符展开成多行", "-rw-r--r-- 1 root root 100 Oct 27 10:36 /a.zip\n-rw-r--r-- 1 root root 200 Oct 27 10:36 /b.zip", 0, false},
		{"字段不足", "-rw-r--r--", 0, false},
	}
	for _, c := range cases {
		got, ok := parseLsFieldSize(c.output)
		if ok != c.ok || got != c.want {
			t.Fatalf("%s: 期望 (%d,%v)，实际 (%d,%v)", c.name, c.want, c.ok, got, ok)
		}
	}
}
