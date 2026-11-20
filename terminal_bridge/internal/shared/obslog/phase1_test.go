package obslog

// 阶段 1 可靠性补齐的回归守卫（2026-11-20）：
//  1) gaps.jsonl 必须在重启时重放，且只重放一次（否则崩溃窗口的缺口永远不上报）；
//  2) stderr 轮转代数必须可读且影响 event_id（否则轮转后偏移复用会被幂等误丢）。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestReplayGapsAfterRestart(t *testing.T) {
	instDir := filepath.Join(t.TempDir(), "inst")
	if err := os.MkdirAll(instDir, 0o700); err != nil {
		t.Fatalf("准备目录失败: %v", err)
	}
	gap := LogEntry{
		Type:    "bridge_log",
		Event:   "log_upload.gap",
		Message: "outbox 预算超限",
		EventID: "gap-1",
	}
	line, err := json.Marshal(gap)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	path := filepath.Join(instDir, gapsFileName)
	if err := os.WriteFile(path, append(line, '\n'), 0o600); err != nil {
		t.Fatalf("写入 gaps 失败: %v", err)
	}

	u := newLogUploader()
	u.instance = "inst"

	got := u.replayGapsLocked(instDir)
	if len(got) != 1 || got[0].EventID != "gap-1" {
		t.Fatalf("重启应重放 1 条 gap，实际 %d 条", len(got))
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("重放后 gaps.jsonl 应被归档移除，否则会重复上报")
	}
	if _, err := os.Stat(filepath.Join(instDir, gapsArchiveName)); err != nil {
		t.Fatal("归档文件应保留，便于事后核对")
	}
	if again := u.replayGapsLocked(instDir); len(again) != 0 {
		t.Fatalf("同一次归档不得重复重放，实际 %d 条", len(again))
	}
}

func TestReplayGapsSkipsCorruptedLines(t *testing.T) {
	instDir := filepath.Join(t.TempDir(), "inst")
	if err := os.MkdirAll(instDir, 0o700); err != nil {
		t.Fatalf("准备目录失败: %v", err)
	}
	gap := LogEntry{Type: "bridge_log", Event: "log_upload.gap", EventID: "gap-ok"}
	line, _ := json.Marshal(gap)
	content := "not-json\n\n" + string(line) + "\n"
	if err := os.WriteFile(filepath.Join(instDir, gapsFileName), []byte(content), 0o600); err != nil {
		t.Fatalf("写入失败: %v", err)
	}

	u := newLogUploader()
	u.instance = "inst"
	got := u.replayGapsLocked(instDir)
	if len(got) != 1 || got[0].EventID != "gap-ok" {
		t.Fatalf("损坏行应跳过、有效行应保留，实际 %d 条", len(got))
	}
}

func TestStderrGenerationRead(t *testing.T) {
	dir := t.TempDir()
	if got := stderrGeneration(dir); got != "0" {
		t.Fatalf("无 gen 文件时应回退 0，实际 %q", got)
	}
	if err := os.WriteFile(filepath.Join(dir, stderrGenFileName), []byte("1712345678901234567\n"), 0o600); err != nil {
		t.Fatalf("写入 gen 失败: %v", err)
	}
	if got := stderrGeneration(dir); got != "1712345678901234567" {
		t.Fatalf("应读取轮转代数，实际 %q", got)
	}
}
