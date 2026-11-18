package main

// S4b 缺陷用例（设计 §六 S4 验收 4/5/6 + 关键约束 B9）：
//   - B6：官方源探测失败/包名解析落空不得回"已是最新"，必须如实"未做升级判定"；
//   - B7：df 失败不得静默跳过空间检查，留痕并降级处理；
//   - B9：包缓存有淘汰，校验基线落账本，桥重启后"重试安装"仍有比对依据。
//
// B8 的缺陷用例在 acli_plugin_verify_test.go（descriptor 多目标落盘验证）。

import (
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"terminal_bridge/internal/shared/reconcile"
)

// ── B6：期望证据缺失不得伪装成"已是最新" ─────────────────────────────────────

func TestAcliSyncDecisionProbeFailureIsDegradedSkip(t *testing.T) {
	// 已安装 + 官方源探测失败（remote 全空）：旧实现落 default 分支回
	// (false, "")→ 前端显示"已是最新"。新实现必须降级 Skip 并留证。
	plan := acliSyncDecision("1.0.3", "2026-10-29 17:00:12", "", "", false, "all")
	if plan.Action != reconcile.ActionSkip || !plan.Degraded || plan.Stage != reconcile.StageProbe {
		t.Fatalf("探测失败必须降级 Skip（degraded/stage=probe），实际 %+v", plan)
	}
	install, event := acliDecisionOutcome(plan, "1.0.3", false)
	if install || event != "probe_failed" {
		t.Fatalf("探测失败应映射为 (false, probe_failed)，实际 (%v, %q)", install, event)
	}

	// 未安装不受源站故障影响：仍须安装（否则"没装"也被静默跳过）。
	plan = acliSyncDecision("", "", "", "", false, "all")
	if plan.Action != reconcile.ActionInstall || plan.Reason != "not_installed" {
		t.Fatalf("未安装必须 Install，实际 %+v", plan)
	}

	// force / 显式重试是用户意图，期望缺失也拦不住。
	for _, c := range []struct {
		force bool
		retry string
	}{
		{true, "all"}, {false, "upload"}, {false, "install"},
	} {
		plan = acliSyncDecision("1.0.3", "2026-10-29 17:00:12", "", "", c.force, c.retry)
		if plan.Action != reconcile.ActionInstall {
			t.Fatalf("force=%v retry=%s 应 Install，实际 %+v", c.force, c.retry, plan)
		}
	}

	// 期望可得且对齐：无降级 Skip（正常"已是最新"路径不受影响）。
	plan = acliSyncDecision("1.0.3", "2026-10-29 17:00:12", "1.0.3", "2026-10-29 19:00:00", false, "all")
	if plan.Action != reconcile.ActionSkip || plan.Degraded {
		t.Fatalf("双对齐应无降级 Skip，实际 %+v", plan)
	}
}

// TestAcliSyncUpToDateMessageHonestOnProbeFailure 钉住外壳文案：
// probe_failed 分支的回执消息不得含"已是最新/为最新版本"，必须如实标注未判定。
func TestAcliSyncUpToDateMessageHonestOnProbeFailure(t *testing.T) {
	raw, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	src := string(raw)
	if !strings.Contains(src, `case "probe_failed":`) {
		t.Fatal("handleAcliSync 必须对 probe_failed 判定留痕（B6）")
	}
	if !strings.Contains(src, "官方源探测失败，未做升级判定") {
		t.Fatal("源探测失败的回执必须如实标注未做升级判定（B6）")
	}
	// 判定必须走骨架：外壳不得再保留手写判序 switch。
	if !strings.Contains(src, "decisionPlan := acliSyncDecision(") {
		t.Fatal("handleAcliSync 的判定必须由 descriptor+Compare 驱动（S4b）")
	}
}

// ── B7：df 失败不得静默跳过空间检查 ──────────────────────────────────────────

func TestAcliDiskSpaceDecision(t *testing.T) {
	cases := []struct {
		name       string
		out        string
		code       int
		runErr     error
		minMB      int
		wantPass   bool
		wantMB     int64
		wantProbed bool
	}{
		{"空间足够", "20480\n", 0, nil, 1024, true, 20480, true},
		{"空间不足", "512\n", 0, nil, 1024, false, 512, true},
		// 旧实现 availableMB>0 条件把 0 空间短路成跳过——明确的空间不足必须拒绝。
		{"零空间也是不足", "0\n", 0, nil, 1024, false, 0, true},
		{"恰好达标", "1024", 0, nil, 1024, true, 1024, true},
		// B7 核心：df 失败 → 降级放行但 probed=false，调用方必须留痕。
		{"df 非零退出", "", 1, nil, 1024, true, 0, false},
		{"df 通道异常", "", -1, errors.New("ssh closed"), 1024, true, 0, false},
		{"输出不可解析", "df: cannot access", 0, nil, 1024, true, 0, false},
		{"负数输出不可信", "-5", 0, nil, 1024, true, 0, false},
	}
	for _, c := range cases {
		pass, mb, probed := acliDiskSpaceDecision(c.out, c.code, c.runErr, c.minMB)
		if pass != c.wantPass || mb != c.wantMB || probed != c.wantProbed {
			t.Fatalf("%s: acliDiskSpaceDecision=(%v,%d,%v) want (%v,%d,%v)",
				c.name, pass, mb, probed, c.wantPass, c.wantMB, c.wantProbed)
		}
	}
}

func TestAcliSyncDiskProbeFailureLeavesTrace(t *testing.T) {
	raw, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	src := string(raw)
	// 留痕 + 降级标注缺一不可：静默跳过正是 B7 的事故形态。
	if !strings.Contains(src, "acli_sync.disk_probe_failed") {
		t.Fatal("df 失败必须 WARN 留痕（B7）")
	}
	if !strings.Contains(src, "本次安装未验证可用空间") {
		t.Fatal("空间未验证必须在成功回执里如实标注（B7）")
	}
}

// ── B9：缓存淘汰 + 基线账本（桥重启不丢比对依据）─────────────────────────────

func TestAcliPackageCacheEvictsExpiredAndOverCapacity(t *testing.T) {
	cache := newAcliPackageCache()
	pkg := testAcliPackage()

	// TTL 清扫：无人问津的过期条目也要在 put 时被淘汰，不能永驻内存。
	cache.items["stale"] = acliPackageCacheEntry{pkg: pkg, cachedAt: time.Now().Add(-acliPackageCacheTTL - time.Minute)}
	cache.put("fresh", pkg)
	if _, ok := cache.items["stale"]; ok {
		t.Fatal("过期条目应在 put 时被清扫（B9）")
	}

	// 上限淘汰：超过 acliPackageCacheMaxEntries 时丢最旧的。
	base := time.Now()
	for i := 0; i < acliPackageCacheMaxEntries; i++ {
		key := string(rune('a' + i))
		cache.items[key] = acliPackageCacheEntry{pkg: pkg, cachedAt: base.Add(time.Duration(i) * time.Second)}
	}
	cache.put("newest", pkg)
	if len(cache.items) > acliPackageCacheMaxEntries {
		t.Fatalf("缓存条目数不得超过上限 %d，实际 %d", acliPackageCacheMaxEntries, len(cache.items))
	}
	if _, ok := cache.items["a"]; ok {
		t.Fatal("超限应淘汰最旧条目")
	}
	if _, ok := cache.items["newest"]; !ok {
		t.Fatal("新写入条目必须保留")
	}
}

func TestAcliPackageBaselineSurvivesBridgeRestart(t *testing.T) {
	store := reconcile.NewStore(t.TempDir())
	pkg := testAcliPackage()

	first := newAcliPackageCache()
	first.store = store
	first.put("case@node", pkg)

	// 模拟桥重启：内存缓存全丢，只剩账本。
	restarted := newAcliPackageCache()
	restarted.store = store
	got := restarted.get("case@node")
	if got == nil {
		t.Fatal("桥重启后应凭账本恢复校验基线（B9）")
	}
	if got.SHA256 != pkg.SHA256 || got.MD5 != pkg.MD5 || got.Size != pkg.Size || got.Filename != pkg.Filename {
		t.Fatalf("基线事实必须与下载时一致：%+v", got)
	}
	if got.Data != nil {
		t.Fatal("基线包不含字节：只能做校验基线，不能当上传内容")
	}
	// 基线足以支撑 install-only 重试的远端包比对。
	if err := verifyReusedRemotePackage(got, pkg.Size, "sha256", pkg.SHA256); err != nil {
		t.Fatalf("恢复的基线应能通过远端包比对：%v", err)
	}

	// 传输失败 drop：基线一并作废，不得让可疑的包凭旧基线通过比对。
	first.drop("case@node")
	if got := restarted.get("case@node"); got != nil {
		t.Fatal("drop 后账本基线也应作废")
	}

	// 过期基线不得复用：官方 latest 可能已换包。
	first.put("case@node", pkg)
	st, err := store.Load(reconcile.KindAcliBinary, acliBaselineLedgerKey("case@node"))
	if err != nil || st == nil {
		t.Fatalf("基线应已落账本：%v", err)
	}
	st.AppliedAt = time.Now().Add(-acliPackageCacheTTL - time.Minute)
	if err := store.Save(st); err != nil {
		t.Fatal(err)
	}
	if got := restarted.get("case@node"); got != nil {
		t.Fatal("过期基线不得复用（会装回旧版本）")
	}
}
