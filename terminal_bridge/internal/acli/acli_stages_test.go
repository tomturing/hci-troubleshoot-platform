package acli

// 阶段 0/1 的判定测试。
//
// 为什么这两处优先补测：它们决定"能不能跳过传输"和"下载哪个架构"，
// 出错时的表现都不是报错，而是"看起来成功但装错了东西"——
// 前者是未校验的快路径，后者是 exec format error 被误读成包损坏。

import (
	"context"
	"errors"
	"strings"
	"terminal_bridge/internal/shared/logcontract"
	"testing"
)

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

// 重跑复用的判定：四种结论必须互斥且可区分。
//
// 这条判定错了不会报错，只会把上一轮已经坏掉的包再执行一遍——
// 所以「与基线不符」必须判成 rejected 而不是"凑合用"。
func TestDecideAcliReuse(t *testing.T) {
	baseline := testAcliPackage()
	matched := acliRemoteProbe{Size: baseline.Size, Algo: "sha256", Digest: baseline.SHA256}

	cases := []struct {
		name   string
		retry  string
		probe  acliRemoteProbe
		base   *acliPackage
		ready  bool
		reason string
	}{
		{"完整流程不探测", "all", acliRemoteProbe{}, nil, false, ""},
		{"探测失败降级", "install", acliRemoteProbe{Err: errors.New("ssh broken")}, baseline, false, "probe_failed"},
		{"包不存在降级", "upload", acliRemoteProbe{Missing: true}, baseline, false, "missing"},
		{"基线与节点一致", "install", matched, baseline, true, "verified"},
		{"本地无基线不可复用", "install", matched, nil, false, "rejected"},
		{"大小不符不可复用", "upload", acliRemoteProbe{Size: baseline.Size + 1, Algo: "sha256", Digest: baseline.SHA256}, baseline, false, "rejected"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := decideAcliReuse(tc.retry, tc.probe, tc.base)
			if got.Ready != tc.ready || got.Reason != tc.reason {
				t.Fatalf("decideAcliReuse()=(ready=%v,reason=%q)，期望 (%v,%q)", got.Ready, got.Reason, tc.ready, tc.reason)
			}
			if tc.reason == "rejected" && got.Detail == "" {
				t.Fatal("rejected 必须带具体不符点，否则现场只能看到「不可复用」")
			}
		})
	}
}

// 安装结论：三种失败必须给出不同文案，否则现场只能看到「安装失败」。
func TestEvalAcliInstallOutcome(t *testing.T) {
	cases := []struct {
		name     string
		out      acliInstallOutcome
		timedOut bool
		ok       bool
		contains string
	}{
		{"成功", acliInstallOutcome{ExitCode: 0}, false, true, ""},
		{"超时", acliInstallOutcome{ExitCode: -1, Err: context.DeadlineExceeded}, true, false, "超时"},
		{"通道中断", acliInstallOutcome{ExitCode: 0, Err: errors.New("eof")}, false, false, "异常中断"},
		{"退出码非零", acliInstallOutcome{ExitCode: 3}, false, false, "退出码 3"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := evalAcliInstallOutcome(tc.out, tc.timedOut)
			if got.OK != tc.ok {
				t.Fatalf("OK=%v，期望 %v", got.OK, tc.ok)
			}
			if tc.ok {
				return
			}
			if got.Code != logcontract.ErrInstallExec {
				t.Fatalf("失败码应为 %s，实际 %q", logcontract.ErrInstallExec, got.Code)
			}
			if !strings.Contains(got.Detail, tc.contains) {
				t.Fatalf("说明 %q 未包含 %q", got.Detail, tc.contains)
			}
		})
	}
}

// 超时且同时带错误时必须报超时：否则现场会去查 SSH 通道，而真正原因是集群分发慢。
func TestEvalAcliInstallOutcomeTimeoutWins(t *testing.T) {
	got := evalAcliInstallOutcome(acliInstallOutcome{ExitCode: 0, Err: errors.New("eof")}, true)
	if got.OK || !strings.Contains(got.Detail, "超时") {
		t.Fatalf("超时应优先于通道中断，实际 %q", got.Detail)
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
