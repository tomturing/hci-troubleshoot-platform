package logcollect

import (
	"os"
	"strings"
	"testing"
)

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
