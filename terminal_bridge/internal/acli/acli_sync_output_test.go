package acli

import (
	"strings"
	"terminal_bridge/internal/shared/remote"
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

// 升级判定必须有序：只有远端日期严格更新才安装，同日或本地更新一律跳过，
// 禁止降级（Q2026111700614：预装 1.0.3 被旧官方包 1.0.0 误判落后并降级）。
func TestRemoteBuildNewer(t *testing.T) {
	cases := []struct {
		name, current, remote string
		want                  bool
	}{
		{"远端跨天更新", "2026-05-20 16:01:31", "2026-05-21 00:00:01", true},
		{"同日不同时刻不升级", "2026-05-20 16:01:31", "2026-05-20 17:46:20", false},
		{"远端更旧禁止降级", "2026-11-09 15:21:16", "2026-05-20 17:33:24", false},
		{"本端为空", "", "2026-05-20 17:46:20", false},
		{"远端为空", "2026-05-20 16:01:31", "", false},
		{"非标准格式无法判序", "abc", "def", false},
		{"非标准格式相同也不升级", "abc", "abc", false},
	}
	for _, c := range cases {
		if got := remoteBuildNewer(c.current, c.remote); got != c.want {
			t.Fatalf("%s: remoteBuildNewer(%q,%q)=%v want %v", c.name, c.current, c.remote, got, c.want)
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
	if got := remote.NormalizeOutputText(gbk); got != text {
		t.Fatalf("GBK 解码结果不符: %q", got)
	}
	// 合法 UTF-8 必须原样返回，避免二次转码
	if got := remote.NormalizeOutputText(text); got != text {
		t.Fatalf("合法 UTF-8 被改写: %q", got)
	}
}

// 只截头部会丢掉安装结论（成功/失败、节点数都在尾部）。
func TestFormatOutputPreviewKeepsTail(t *testing.T) {
	body := strings.Repeat("A", 2000) + "节点 172.28.24.132 安装成功"
	got := remote.FormatOutputPreview(body, 128, 128)
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
	if short := remote.FormatOutputPreview("ok", 128, 128); short != "ok" {
		t.Fatalf("短输出被改写: %q", short)
	}
}

// 包名解析只认现行命名约定 acli_<版本>_<架构>_<YYYYMMDDHHMMSS>.acli。
// 旧格式（acli-installer_<架构>_YYYY-MM-DD_HH-MM-SS.acli）已随发布侧改名退役：
// 解析落空必须返回空串交调用方留痕+保守跳过，而不是猜一个时间戳进判序
// （Q2026101401677：旧正则对新包名落空 → 静默"已是最新"，1.0.3 发布后节点仍 1.0.0）。
func TestParseAcliRemoteName(t *testing.T) {
	cases := []struct {
		name, filename, wantVersion, wantBuild string
	}{
		{"现行 x86/c86 合包名", "acli_1.0.3_x86-c86_64_20261029170012.acli", "1.0.3", "2026-10-29 17:00:12"},
		{"现行 aarch64 包名", "acli_1.0.3_aarch64_20261029165837.acli", "1.0.3", "2026-10-29 16:58:37"},
		{"旧格式退役不落兜底", "acli-installer_x86_64_2026-05-20_17-33-24.acli", "", ""},
		{"无时间戳不猜", "acli_1.0.3_x86_64.acli", "1.0.3", ""},
		{"乱码名全落空", "evil../../passwd.acli", "", ""},
		{"空名", "", "", ""},
	}
	for _, c := range cases {
		version, build := parseAcliRemoteName(c.filename)
		if version != c.wantVersion || build != c.wantBuild {
			t.Fatalf("%s: parseAcliRemoteName(%q)=(%q,%q) want (%q,%q)",
				c.name, c.filename, version, build, c.wantVersion, c.wantBuild)
		}
	}
}

// 版本判序必须逐段数值比较：字符串序会把 1.0.10 判得比 1.0.9 旧。
func TestCompareAcliVersions(t *testing.T) {
	cases := []struct {
		name, a, b string
		want       int
	}{
		{"升级", "1.0.0", "1.0.3", -1},
		{"降级", "1.0.3", "1.0.0", 1},
		{"双位段数值序", "1.0.9", "1.0.10", -1},
		{"相等", "1.0.3", "1.0.3", 0},
		{"段数不齐补零", "1.0", "1.0.0", 0},
		{"非数字不做部分比较", "1.x.0", "1.0.3", 0},
	}
	for _, c := range cases {
		if got := compareAcliVersions(c.a, c.b); got != c.want {
			t.Fatalf("%s: compareAcliVersions(%q,%q)=%d want %d", c.name, c.a, c.b, got, c.want)
		}
	}
}

// 安装判定：版本优先、日期兜底、强制信号先行、降级永不自动发生。
// 关键回归：旧版本用新日期重打包上架（1.0.0 + 今天日期 vs 本地 1.0.3）——
// 纯日期判据会判"远端更新"再次降级（Q2026111700614 同形），版本优先必须拦住。
func TestAcliInstallDecision(t *testing.T) {
	cases := []struct {
		name                                             string
		currentVer, currentBuild, remoteVer, remoteBuild string
		force                                            bool
		retryStage                                       string
		wantInstall                                      bool
		wantEvent                                        string
	}{
		{"未安装强制装", "", "", "1.0.3", "2026-10-29 17:00:12", false, "all", true, "not_installed"},
		{"force 先行", "1.0.3", "2026-10-29 17:00:12", "1.0.0", "2026-05-20 17:33:24", true, "all", true, "force_install"},
		{"显式重试先行", "1.0.3", "2026-10-29 17:00:12", "1.0.3", "2026-10-29 17:00:12", false, "upload", true, "retry_requested"},
		{"版本升级", "1.0.0", "2026-05-20 17:22:16", "1.0.3", "2026-10-29 17:00:12", false, "all", true, "version_outdated"},
		{"旧版本新日期重打包不降级", "1.0.3", "2026-10-29 17:00:12", "1.0.0", "2026-11-17 12:00:00", false, "all", false, "downgrade_blocked"},
		{"同版本跨天重打包升级", "1.0.3", "2026-10-29 17:00:12", "1.0.3", "2026-10-30 09:00:00", false, "all", true, "version_outdated"},
		{"同版本同日不升级", "1.0.3", "2026-10-29 17:00:12", "1.0.3", "2026-10-29 19:00:00", false, "all", false, ""},
		{"同版本旧日期不降级", "1.0.3", "2026-10-29 17:00:12", "1.0.3", "2026-10-28 09:00:00", false, "all", false, "downgrade_blocked"},
		{"期望证据缺失降级跳过(B6)", "1.0.0", "2026-05-20 17:22:16", "", "", false, "all", false, "probe_failed"},
		{"版本落空日期兜底升级", "1.0.0", "2026-05-20 17:22:16", "", "2026-10-29 17:00:12", false, "all", true, "version_outdated"},
	}
	for _, c := range cases {
		install, event := acliInstallDecision(c.currentVer, c.currentBuild, c.remoteVer, c.remoteBuild, c.force, c.retryStage)
		if install != c.wantInstall || event != c.wantEvent {
			t.Fatalf("%s: acliInstallDecision(...)=(%v,%q) want (%v,%q)",
				c.name, install, event, c.wantInstall, c.wantEvent)
		}
	}
}
