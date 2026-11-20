package remote

import (
	"strings"
	"testing"
)

func TestBuildChunkedArgvWriteCommandsSplitsByEscapedBudget(t *testing.T) {
	// 内容全是单引号：shell 转义后每个字符膨胀到 4 字节，是最坏情况。
	content := strings.Repeat("'", 100_000)
	spec := WriteSpec{
		Path:   "/tmp/script.py",
		Data:   []byte(content),
		Size:   int64(len(content)),
		SHA256: "deadbeef",
		Mode:   "0755",
	}
	commands := BuildChunkedArgvWriteCommands(spec, 16<<10)
	if len(commands) < 7 {
		t.Fatalf("期望至少 7 条命令（100KB/16KB），实际 %d", len(commands))
	}
	for index, command := range commands {
		// 128KB 是 Linux 单参数上限；留 8KB 给 `printf '%s' ` 与重定向外壳。
		if len(command) > 120<<10 {
			t.Fatalf("第 %d 条命令 %d 字节，接近 argv 上限", index, len(command))
		}
	}
	// 最后一条必须只做校验+替换，不能再次携带内容。
	last := commands[len(commands)-1]
	if !strings.Contains(last, WriteOKMark) || strings.Contains(last, "99999") {
		t.Fatalf("最后一条命令不符合「只校验+mv」约定：%s", last)
	}
}

func TestBuildChunkedArgvWriteCommandsKeepsUTF8(t *testing.T) {
	content := strings.Repeat("采集脚本", 10_000)
	commands := BuildChunkedArgvWriteCommands(WriteSpec{
		Path: "/tmp/a.sh", Data: []byte(content), Size: int64(len(content)), Mode: "0755",
	}, 1024)
	var rebuilt strings.Builder
	for index, command := range commands[:len(commands)-1] {
		operator := ">>"
		if index == 0 {
			operator = ">"
		}
		start := strings.Index(command, "printf '%s' ")
		end := strings.Index(command, " "+operator+" ")
		if start < 0 || end < 0 {
			t.Fatalf("命令形态不符合预期：%s", command)
		}
		rebuilt.WriteString(unquoteForTest(t, command[start+len("printf '%s' "):end]))
	}
	if rebuilt.String() != content {
		t.Fatalf("分块重拼后内容不一致：期望 %d 字节，实际 %d 字节", len(content), rebuilt.Len())
	}
}

// unquoteForTest 只解 ShellQuote 产生的最简形式，够用于断言内容未丢字节。
func unquoteForTest(t *testing.T, quoted string) string {
	t.Helper()
	if !strings.HasPrefix(quoted, "'") || !strings.HasSuffix(quoted, "'") {
		t.Fatalf("不是单引号包裹的字面量：%s", quoted)
	}
	return strings.ReplaceAll(quoted[1:len(quoted)-1], `'\''`, "'")
}
