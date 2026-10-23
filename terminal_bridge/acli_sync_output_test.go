package main

import (
	"strings"
	"testing"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
	"golang.org/x/text/transform"
)

// 版本判定只比日期：编译时间与打包时间不同源，同日必须视为同一版本，
// 否则每次登录都会重装（实测相差 1h45m，恒定判定落后）。
func TestSameBuildDate(t *testing.T) {
	cases := []struct {
		name, current, remote string
		want                  bool
	}{
		{"同一天不同时刻", "2026-05-20 16:01:31", "2026-05-20 17:46:20", true},
		{"跨天", "2026-05-19 23:59:59", "2026-05-20 00:00:01", false},
		{"本端为空", "", "2026-05-20 17:46:20", false},
		{"远端为空", "2026-05-20 16:01:31", "", false},
		{"非标准格式按原值比较", "abc", "abc", true},
	}
	for _, c := range cases {
		if got := sameBuildDate(c.current, c.remote); got != c.want {
			t.Fatalf("%s: sameBuildDate(%q,%q)=%v want %v", c.name, c.current, c.remote, got, c.want)
		}
	}
}

func TestBuildDate(t *testing.T) {
	if got := buildDate("2026-05-20 16:01:31"); got != "2026-05-20" {
		t.Fatalf("buildDate=%q", got)
	}
	if got := buildDate(""); got != "" {
		t.Fatalf("buildDate(empty)=%q", got)
	}
}

// 安装器输出可能是 GBK：不修编码会在 JSON 里变成一串 U+FFFD，排障信息全丢。
func TestNormalizeOutputTextDecodesGBK(t *testing.T) {
	const text = "节点 172.28.24.132 安装成功"
	gbk, _, err := transform.String(simplifiedchinese.GB18030.NewEncoder(), text)
	if err != nil {
		t.Fatalf("构造 GBK 样本失败: %v", err)
	}
	if utf8.ValidString(gbk) {
		t.Skip("样本恰好是合法 UTF-8，跳过编码断言")
	}
	if got := normalizeOutputText(gbk); got != text {
		t.Fatalf("GBK 解码结果不符: %q", got)
	}
	// 合法 UTF-8 必须原样返回，避免二次转码
	if got := normalizeOutputText(text); got != text {
		t.Fatalf("合法 UTF-8 被改写: %q", got)
	}
}

// 只截头部会丢掉安装结论（成功/失败、节点数都在尾部）。
func TestFormatOutputPreviewKeepsTail(t *testing.T) {
	body := strings.Repeat("A", 2000) + "节点 172.28.24.132 安装成功"
	got := formatOutputPreview(body, 128, 128)
	if !utf8.ValidString(got) {
		t.Fatalf("预览不是合法 UTF-8")
	}
	if !strings.HasPrefix(got, "AAAA") {
		t.Fatalf("头部丢失: %q", got[:20])
	}
	if !strings.HasSuffix(got, "安装成功") {
		t.Fatalf("尾部结论丢失: %q", got[len(got)-40:])
	}
	if !strings.Contains(got, "省略") {
		t.Fatalf("缺少截断标记")
	}
	// 短输出不应被改动
	if short := formatOutputPreview("ok", 128, 128); short != "ok" {
		t.Fatalf("短输出被改写: %q", short)
	}
}
