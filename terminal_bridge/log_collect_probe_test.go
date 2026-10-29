package main

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
		got := probeLogCollectArchive(context.Background(), nil, rawPath)
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
