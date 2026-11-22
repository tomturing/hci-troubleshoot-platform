package acli

// 阶段 0/1 的判定测试。
//
// 为什么这两处优先补测：它们决定"能不能跳过传输"和"下载哪个架构"，
// 出错时的表现都不是报错，而是"看起来成功但装错了东西"——
// 前者是未校验的快路径，后者是 exec format error 被误读成包损坏。

import "testing"

func TestNormalizeAcliRetryStage(t *testing.T) {
	cases := map[string]string{
		"":             "all",
		"all":          "all",
		"upload":       "upload",
		"UPLOAD":       "upload",
		" upload ":     "upload",
		"install":      "install",
		"Install":      "install",
		"install\n":    "install",
		"retry":        "all", // 未知值必须回 all：不能猜成 install（会跳过整个传输）
		"uplaod":       "all",
		"upload_force": "all",
	}
	for raw, want := range cases {
		if got := normalizeAcliRetryStage(raw); got != want {
			t.Fatalf("normalizeAcliRetryStage(%q)=%q，期望 %q", raw, got, want)
		}
	}
}

func TestParseTargetArch(t *testing.T) {
	cases := []struct {
		in      string
		want    string
		support bool
	}{
		{"x86_64\n", "x86_64", true},
		{"AMD64\n", "x86_64", true},
		{"x86_64", "x86_64", true},
		{"aarch64\n", "aarch64", true},
		{"arm64", "aarch64", true},
		{"ARM64 ", "aarch64", true},
		{"ppc64le\n", "ppc64le", false},
		{"riscv64", "riscv64", false},
		{"", "", false},
	}
	for _, tc := range cases {
		got, ok := parseTargetArch(tc.in)
		if got != tc.want || ok != tc.support {
			t.Fatalf("parseTargetArch(%q)=(%q,%v)，期望 (%q,%v)", tc.in, got, ok, tc.want, tc.support)
		}
	}
}

// 不支持时返回的 arch 会被直接写进回执展示给用户，
// 必须是清洗过的原文，不能是空串——否则界面只说"暂不支持的硬件架构: "。
func TestParseTargetArchKeepsRawWhenUnsupported(t *testing.T) {
	got, ok := parseTargetArch("  PPC64LE  ")
	if ok {
		t.Fatal("ppc64le 不应被判为支持")
	}
	if got != "ppc64le" {
		t.Fatalf("不支持时回执应展示清洗后的原文，实际 %q", got)
	}
}
