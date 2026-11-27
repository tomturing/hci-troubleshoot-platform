package logcollect

import (
	"context"
	"strings"
	"testing"
)

// 本组用例锁死 Q2026112700769 的回归边界：同一工单取消后换模块重收，固定归档路径上
// 还压着上一批次的 4.17 GiB 旧包，进度探测把它当成本次 archive_size 上报，平台据此
// 把新包（146 MiB）判成"体积不一致"。

type fakeBaselineRunner struct {
	output   string
	exitCode int
	err      error
	commands []string
}

func (f *fakeBaselineRunner) RunQuickCommand(_ context.Context, cmd string) (string, int, error) {
	f.commands = append(f.commands, cmd)
	return f.output, f.exitCode, f.err
}

func TestParseArchiveStatTriple(t *testing.T) {
	cases := []struct {
		name     string
		output   string
		wantOK   bool
		wantSize int64
	}{
		{name: "标准三段", output: "4478420847|1795759133|123456\n", wantOK: true, wantSize: 4478420847},
		{name: "0 字节包也能量出来", output: "0|1795759133|99", wantOK: true, wantSize: 0},
		{name: "受限 shell 回显格式串", output: "%s|%Y|%i", wantOK: false},
		{name: "只有体积（旧格式）", output: "29900423", wantOK: false},
		{name: "段数不足", output: "29900423|1795759133", wantOK: false},
		{name: "inode 非法", output: "29900423|1795759133|abc", wantOK: false},
		{name: "mtime 非法", output: "29900423|x|123", wantOK: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			size, _, _, ok := parseArchiveStatTriple(tc.output)
			if ok != tc.wantOK {
				t.Fatalf("ok=%v want=%v", ok, tc.wantOK)
			}
			if ok && size != tc.wantSize {
				t.Fatalf("size=%d want=%d", size, tc.wantSize)
			}
		})
	}
}

func TestLogCollectArchiveIsResidualDetectsUnchangedFile(t *testing.T) {
	const execID = "task-residual-1"
	const caseID = "Q2026112700769"
	path := fallbackLogCollectArchivePath(caseID)
	logCollectArchiveBaselines.Store(execID, archiveBaseline{
		Path: path, Size: 4478420847, ModUnix: 1795759133, Inode: 555,
	})

	ctx := context.Background()
	same := &fakeBaselineRunner{output: "4478420847|1795759133|555\n"}
	if !logCollectArchiveIsResidual(ctx, same, execID, path, 4478420847) {
		t.Fatal("完全一致的文件必须判为残留，否则旧包会被当成本次体积")
	}
	if len(same.commands) != 1 || !strings.Contains(same.commands[0], "stat -c") {
		t.Fatalf("应只发一条只读 stat，实际 %v", same.commands)
	}

	t.Run("体积变了就是本次重写过", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "152850599|1795759133|555\n"}
		if logCollectArchiveIsResidual(ctx, runner, execID, path, 152850599) {
			t.Fatal("体积不同不可能是同一份残留")
		}
	})

	t.Run("体积相同但 mtime 变了也不判残留", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "4478420847|1795759999|555\n"}
		if logCollectArchiveIsResidual(ctx, runner, execID, path, 4478420847) {
			t.Fatal("mtime 变了说明文件被写过，不能算残留")
		}
	})

	t.Run("体积与 mtime 相同但 inode 变了也不判残留", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "4478420847|1795759133|777\n"}
		if logCollectArchiveIsResidual(ctx, runner, execID, path, 4478420847) {
			t.Fatal("inode 变了是另一个文件，不能算残留")
		}
	})

	t.Run("量不到当前身份时保守上报", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "", exitCode: 127, err: nil}
		if logCollectArchiveIsResidual(ctx, runner, execID, path, 4478420847) {
			t.Fatal("量不到就不能断言残留，否则会把本次真包静默掉")
		}
	})

	t.Run("路径不一致不算残留", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "4478420847|1795759133|555\n"}
		other := fallbackLogCollectArchivePath("Q2026112700770")
		if logCollectArchiveIsResidual(ctx, runner, execID, other, 4478420847) {
			t.Fatal("基线属于另一条路径时不能套用")
		}
	})

	t.Run("没有基线的任务永不判残留", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "4478420847|1795759133|555\n"}
		if logCollectArchiveIsResidual(ctx, runner, "task-without-baseline", path, 4478420847) {
			t.Fatal("旧版本桥没有基线，必须保持原来的上报行为")
		}
	})
}

func TestRememberLogCollectArchiveBaseline(t *testing.T) {
	ctx := context.Background()
	const caseID = "Q2026112700771"

	t.Run("盘上已有包时登记基线", func(t *testing.T) {
		const execID = "task-baseline-1"
		runner := &fakeBaselineRunner{output: "4478420847|1795759133|555\n"}
		rememberLogCollectArchiveBaseline(ctx, runner, execID, caseID, "10.0.0.1", "trace", "")
		stored, ok := logCollectArchiveBaselines.Load(execID)
		if !ok {
			t.Fatal("应登记基线，否则后面无法识别残留")
		}
		base := stored.(archiveBaseline)
		if base.Size != 4478420847 || base.Inode != 555 {
			t.Fatalf("基线字段不对：%+v", base)
		}
		if base.Path != fallbackLogCollectArchivePath(caseID) {
			t.Fatalf("基线路径必须是固定归档路径：%s", base.Path)
		}
	})

	t.Run("没有包时不登记（常态，不产生噪音）", func(t *testing.T) {
		const execID = "task-baseline-2"
		runner := &fakeBaselineRunner{output: "", exitCode: 1}
		rememberLogCollectArchiveBaseline(ctx, runner, execID, caseID, "10.0.0.1", "trace", "")
		if _, ok := logCollectArchiveBaselines.Load(execID); ok {
			t.Fatal("没有残留文件时不应登记基线")
		}
	})

	t.Run("缺任务ID时不登记", func(t *testing.T) {
		runner := &fakeBaselineRunner{output: "1|1795759133|1\n"}
		rememberLogCollectArchiveBaseline(ctx, runner, "", caseID, "10.0.0.1", "trace", "")
		if len(runner.commands) != 0 {
			t.Fatal("没有任务 ID 就没有归属，不该白跑一次 stat")
		}
	})
}

func TestMarkArchiveResidualNotified(t *testing.T) {
	if !markArchiveResidualNotified("task-notice-1") {
		t.Fatal("首次应允许告警")
	}
	if markArchiveResidualNotified("task-notice-1") {
		t.Fatal("同一任务只允许一条残留告警")
	}
	if markArchiveResidualNotified("") {
		t.Fatal("空任务 ID 不应占用去重键")
	}
}
