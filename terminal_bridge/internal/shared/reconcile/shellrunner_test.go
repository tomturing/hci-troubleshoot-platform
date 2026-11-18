package reconcile

// shellrunner_test.go —— 用**真实本地 shell** 当节点模拟器。
//
// 为什么 S3 的验收不用内存模拟：activate 的不变量是"删除只在备份存在之后发生"，
// 它依赖 cp/mv/cmp/rm 的真实语义与真实退出码。用正则模拟这些命令，等于用
// 自己对 shell 的理解去验证自己对 shell 的理解—— backup 不存在时 rm -rf
// 到底会不会执行，只有真 shell 能回答。
//
// 路径一律落在 t.TempDir() 里：测试跑的是真删除，绝不能指向仓库或系统目录。

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
)

// shellRunner 把 remote.Runner 适配到本地 /bin/sh。
// 实现 StreamsRunner，让 remote.RunSplit 走真分轨而不是降级合并。
type shellRunner struct {
	mu   sync.Mutex
	cmds []string
}

func (s *shellRunner) record(command string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cmds = append(s.cmds, command)
}

func (s *shellRunner) commands() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, len(s.cmds))
	copy(out, s.cmds)
	return out
}

func (s *shellRunner) RunQuickCommandStreams(ctx context.Context, command string) (string, string, int, error) {
	s.record(command)
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", command)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := exitCodeOf(err)
	return stdout.String(), stderr.String(), code, err
}

func (s *shellRunner) RunQuickCommand(ctx context.Context, command string) (string, int, error) {
	stdout, stderr, code, err := s.RunQuickCommandStreams(ctx, command)
	if stdout == "" {
		stdout = stderr // 历史契约：stdout 为空时用 stderr 兜底
	}
	return stdout, code, err
}

func (s *shellRunner) WriteStdin(ctx context.Context, command string, r io.Reader) (string, int, error) {
	s.record(command)
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", command)
	cmd.Stdin = r
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := exitCodeOf(err)
	out := stdout.String()
	if out == "" {
		out = stderr.String()
	}
	return out, code, err
}

// exitCodeOf 把执行错误折成退出码：-1 表示通道异常（没有退出码可言），
// 与 main 侧 sshRunner 的约定一致。
func exitCodeOf(err error) int {
	if err == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode()
	}
	return -1
}

// requireShell 在缺少必需工具时跳过：CI（ubuntu）与 HCI 宿主机都具备 cp/mv/cmp，
// 但开发机可能是裁剪环境，缺工具时给出明确跳过而不是假失败。
func requireShell(t *testing.T, tools ...string) {
	t.Helper()
	for _, tool := range tools {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("环境缺 %s，跳过真 shell 回归", tool)
		}
	}
}

// writeFile 在测试目录里落一份文件（模拟节点上的原件/平台清单）。
func writeFile(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
}

func readLocalFile(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读 %s 失败：%v", path, err)
	}
	return string(raw)
}

func hasCommand(cmds []string, fragment string) bool {
	for _, cmd := range cmds {
		if strings.Contains(cmd, fragment) {
			return true
		}
	}
	return false
}
