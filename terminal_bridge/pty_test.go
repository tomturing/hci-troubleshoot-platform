package main

import (
	"encoding/json"
	"io"
	"strings"
	"testing"
)

// TestShouldUsePty 锁定 PTY 判定语义：
//   - acli 前缀命令默认分配伪终端（插件在容器内 exec 时检查 stdin 是否为 tty）；
//   - 显式 pty=true 可对任意命令强制开启；
//   - 其他命令保持 pipe 语义，避免无谓破坏 stdout/stderr 双通道。
func TestShouldUsePty(t *testing.T) {
	cases := []struct {
		name     string
		command  string
		explicit bool
		want     bool
	}{
		{"acli 插件命令", "acli log_collect progress", false, true},
		{"acli 裸命令", "acli", false, true},
		{"acli 前有空白", "   acli --version", false, true},
		{"acli 带换行", "acli log_collect create\n", false, true},
		{"普通命令", "ls -la", false, false},
		{"前缀相似但不匹配", "aclix foo", false, false},
		{"空命令", "", false, false},
		{"显式强制开启", "ls -la", true, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := shouldUsePty(tc.command, tc.explicit); got != tc.want {
				t.Fatalf("shouldUsePty(%q, %v) = %v, want %v", tc.command, tc.explicit, got, tc.want)
			}
		})
	}
}

// chunkedReader 按预设分块返回数据，用于验证跨 Read 边界的 CRLF。
type chunkedReader struct {
	chunks [][]byte
	index  int
}

func (c *chunkedReader) Read(p []byte) (int, error) {
	if c.index >= len(c.chunks) {
		return 0, io.EOF
	}
	n := copy(p, c.chunks[c.index])
	c.index++
	return n, nil
}

func readAllPty(t *testing.T, src io.Reader) string {
	t.Helper()
	got, err := io.ReadAll(&crlfNormalizingReader{src: src})
	if err != nil {
		t.Fatalf("读取失败: %v", err)
	}
	return string(got)
}

// TestCRLFNormalizingReader 覆盖 PTY 输出的换行归一化：
// 单块 CRLF、跨块 CRLF、孤立 CR、EOF 悬空 CR 都必须不丢字节。
func TestCRLFNormalizingReader(t *testing.T) {
	cases := []struct {
		name   string
		chunks []string
		want   string
	}{
		{"单块 CRLF", []string{"a\r\nb\r\n"}, "a\nb\n"},
		{"跨块 CRLF", []string{"a\r", "\nb\r\n"}, "a\nb\n"},
		{"孤立 CR 保留", []string{"a\rb"}, "a\rb"},
		{"EOF 悬空 CR", []string{"a\r"}, "a\r"},
		{"无换行", []string{"plain"}, "plain"},
		{"多个跨块", []string{"x\r", "\n", "y\r", "\n"}, "x\ny\n"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			chunks := make([][]byte, 0, len(tc.chunks))
			for _, chunk := range tc.chunks {
				chunks = append(chunks, []byte(chunk))
			}
			if got := readAllPty(t, &chunkedReader{chunks: chunks}); got != tc.want {
				t.Fatalf("归一化结果 %q, want %q", got, tc.want)
			}
		})
	}
}

func TestNormalizePtyOutputIdempotent(t *testing.T) {
	once := normalizePtyOutput("a\r\nb\r\n")
	if once != "a\nb\n" {
		t.Fatalf("首次归一化结果 %q", once)
	}
	if twice := normalizePtyOutput(once); twice != once {
		t.Fatalf("归一化不幂等: %q -> %q", once, twice)
	}
}

// TestPtySemanticsSerialization 保证前端能区分"stderr 为空"的两种语义。
func TestPtySemanticsSerialization(t *testing.T) {
	pty := OutMessage{Type: "exec_result", ExitCode: 0, PtyMode: "pty", StderrPtyMerged: true}
	encoded, err := json.Marshal(pty)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	for _, fragment := range []string{`"pty_mode":"pty"`, `"stderr_pty_merged":true`} {
		if !strings.Contains(string(encoded), fragment) {
			t.Fatalf("缺少字段 %s，实际: %s", fragment, encoded)
		}
	}

	pipe := OutMessage{Type: "exec_result", ExitCode: 0, PtyMode: "pipe"}
	encodedPipe, err := json.Marshal(pipe)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	if strings.Contains(string(encodedPipe), "stderr_pty_merged") {
		t.Fatalf("pipe 模式不应声明 stderr 合并，实际: %s", encodedPipe)
	}
}

func TestInMessagePtyField(t *testing.T) {
	var withPty InMessage
	if err := json.Unmarshal([]byte(`{"type":"ssh_exec_process","pty":true}`), &withPty); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	if !withPty.Pty {
		t.Fatal("pty 字段未生效")
	}

	var withoutPty InMessage
	if err := json.Unmarshal([]byte(`{"type":"ssh_exec_process"}`), &withoutPty); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	if withoutPty.Pty {
		t.Fatal("缺省时 pty 必须为 false，保持旧客户端行为")
	}
}
