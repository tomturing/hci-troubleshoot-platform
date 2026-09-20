package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestParseLogLevelWeights 校验级别权重与未知级别兜底。
func TestParseLogLevelWeights(t *testing.T) {
	cases := map[string]int{
		"DEBUG":   0,
		"INFO":    1,
		"WARN":    2,
		"WARNING": 2,
		"ERROR":   3,
		"unknown": 1,
	}
	for level, want := range cases {
		if got := parseLogLevel(level); got != want {
			t.Fatalf("parseLogLevel(%q) = %d, 期望 %d", level, got, want)
		}
	}
}

// TestLogHubLevelEnabled 校验阈值过滤：低于阈值的日志不落盘、不回采。
func TestLogHubLevelEnabled(t *testing.T) {
	h := &LogHub{minLevel: parseLogLevel("WARN")}
	if h.levelEnabled("INFO") {
		t.Fatal("WARN 阈值下 INFO 应被过滤")
	}
	if !h.levelEnabled("ERROR") {
		t.Fatal("WARN 阈值下 ERROR 不应被过滤")
	}
	if h.logLevelName() != "WARN" {
		t.Fatalf("logLevelName() = %q, 期望 WARN", h.logLevelName())
	}

	debug := &LogHub{minLevel: parseLogLevel("DEBUG")}
	if !debug.levelEnabled("DEBUG") {
		t.Fatal("DEBUG 阈值下 DEBUG 不应被过滤")
	}
}

// TestResolveDesktopLogDirPrefersDesktop 校验桌面存在时优先落盘到桌面目录。
func TestResolveDesktopLogDirPrefersDesktop(t *testing.T) {
	tmp := t.TempDir()
	if err := os.MkdirAll(filepath.Join(tmp, "Desktop"), 0o755); err != nil {
		t.Fatalf("准备桌面目录失败: %v", err)
	}
	t.Setenv("HOME", tmp)
	t.Setenv("USERPROFILE", tmp)

	dir, source := resolveDesktopLogDir()
	if source != "desktop" {
		t.Fatalf("source = %q, 期望 desktop", source)
	}
	if !strings.HasSuffix(filepath.Clean(dir), filepath.Join("Desktop", "HCI-TerminalBridge-Logs")) {
		t.Fatalf("dir = %q, 期望位于桌面下的 HCI-TerminalBridge-Logs", dir)
	}
}

// TestResolveDesktopLogDirFallback 校验桌面不可用（或不可创建）时回退到可写目录。
func TestResolveDesktopLogDirFallback(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	t.Setenv("USERPROFILE", tmp)
	t.Setenv("XDG_CACHE_HOME", filepath.Join(tmp, "cache"))

	dir, source := resolveDesktopLogDir()
	if dir == "" {
		t.Fatal("桌面不可用时必须回退到可写目录")
	}
	if !strings.HasPrefix(source, "fallback") {
		t.Fatalf("source = %q, 期望 fallback:*", source)
	}
}

// TestLatestBridgeLogFileSkipsCurrent 校验回放只选取上一次运行的日志文件。
func TestLatestBridgeLogFileSkipsCurrent(t *testing.T) {
	dir := t.TempDir()
	previous := filepath.Join(dir, "bridge-20260101-aaaaaaaa.log")
	if err := os.WriteFile(previous, []byte("{}\n"), 0o600); err != nil {
		t.Fatalf("写入历史日志失败: %v", err)
	}
	past := time.Now().Add(-2 * time.Hour)
	if err := os.Chtimes(previous, past, past); err != nil {
		t.Fatalf("调整历史日志时间失败: %v", err)
	}
	current := filepath.Join(dir, "bridge-20260920-bbbbbbbb.log")
	if err := os.WriteFile(current, []byte(""), 0o600); err != nil {
		t.Fatalf("写入当前日志失败: %v", err)
	}

	if got := latestBridgeLogFile(dir, current); got != previous {
		t.Fatalf("latestBridgeLogFile() = %q, 期望 %q", got, previous)
	}
}

// TestReplayLocalLogFileRestoresRing 校验进程重启后能从上次日志文件恢复环形缓冲与 seq。
func TestReplayLocalLogFileRestoresRing(t *testing.T) {
	dir := t.TempDir()
	previous := filepath.Join(dir, "bridge-20260101-aaaaaaaa.log")
	content := "{\"type\":\"bridge_log\",\"seq\":7,\"level\":\"INFO\",\"event\":\"exec.done\",\"message\":\"ok\"}\n" +
		"not-a-json-line\n"
	if err := os.WriteFile(previous, []byte(content), 0o600); err != nil {
		t.Fatalf("写入历史日志失败: %v", err)
	}
	h := &LogHub{cap: 10}
	current := filepath.Join(dir, "bridge-20260102-bbbbbbbb.log")
	if err := os.WriteFile(current, []byte(""), 0o600); err != nil {
		t.Fatalf("写入当前日志失败: %v", err)
	}

	if replayed := h.replayLocalLogFile(dir, current); replayed != 1 {
		t.Fatalf("replayLocalLogFile() = %d, 期望 1（非法行应跳过）", replayed)
	}
	if len(h.ring) != 1 || h.ring[0].Seq != 7 {
		t.Fatalf("回放结果异常: ring=%d seq=%d", len(h.ring), h.seq)
	}
}

// TestLogFileNameLockedIncludesDateAndInstance 校验文件名含日期与实例短标识，便于人工识别与上传。
func TestLogFileNameLockedIncludesDateAndInstance(t *testing.T) {
	h := &LogHub{instanceID: "12345678-abcd-efgh"}
	name := h.logFileNameLocked()
	if !strings.HasPrefix(name, "bridge-") || !strings.HasSuffix(name, ".log") {
		t.Fatalf("文件名格式异常: %q", name)
	}
	if !strings.Contains(name, "-12345678") {
		t.Fatalf("文件名缺少实例短标识: %q", name)
	}
}
