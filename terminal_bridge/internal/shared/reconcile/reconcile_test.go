package reconcile

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func inlineFile(path string, size int) FileSpec {
	return FileSpec{Path: path, Source: Source{Kind: SourceInline, Data: make([]byte, size)}}
}

func baseDesired(files ...FileSpec) DesiredState {
	return DesiredState{Kind: KindConfigFile, NodeIP: "172.28.24.1", Files: files,
		Version: VersionSpec{Mode: VersionNone}}
}

func TestNormalizeRejectsUnknownKindAndNode(t *testing.T) {
	if _, _, err := Normalize(DesiredState{Kind: "whatever", NodeIP: "1.2.3.4"}); err == nil {
		t.Fatal("未知 kind 必须拒绝")
	}
	if _, _, err := Normalize(DesiredState{Kind: KindConfigFile}); err == nil {
		t.Fatal("缺少节点必须拒绝")
	}
}

// 补丁 5：Metadata key 必须登记，未登记一律拒绝（逃生舱一经滥用收口目标失效）。
func TestNormalizeRejectsUnregisteredMetadataKey(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.json", 10))
	d.Metadata = map[string]string{"force_install": "true"}
	if _, _, err := Normalize(d); err == nil {
		t.Fatal("未登记的 Metadata key 必须拒绝")
	}
	d.Metadata = map[string]string{MDKeyTaskID: "task-1"}
	if _, _, err := Normalize(d); err != nil {
		t.Fatalf("已登记 key 应放行：%v", err)
	}
}

func TestNormalizeRejectsBadPathAndSource(t *testing.T) {
	cases := []DesiredState{
		baseDesired(FileSpec{Path: "relative.json", Source: Source{Kind: SourceInline}}),
		baseDesired(FileSpec{Path: "/sf/../etc/passwd", Source: Source{Kind: SourceInline}}),
		baseDesired(FileSpec{Path: "/sf/a.json", Source: Source{Kind: "magic"}}),
		baseDesired(FileSpec{Path: "/sf/a.json", Source: Source{Kind: SourcePackage}}),
		baseDesired(FileSpec{Path: "/sf/a.json", Source: Source{Kind: SourceRemote}}),
	}
	for i, d := range cases {
		if _, _, err := Normalize(d); err == nil {
			t.Fatalf("第 %d 例应被拒绝", i+1)
		}
	}
}

// inline 的 Size/SHA256 由字节自算；声明值与字节不符必须拒绝，
// 否则"摘要自洽"从一开始就是假的。
func TestNormalizeComputesAndValidatesInlineDigest(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.json", 32))
	out, _, err := Normalize(d)
	if err != nil {
		t.Fatalf("归一化失败：%v", err)
	}
	if out.Files[0].Size != 32 || len(out.Files[0].SHA256) != 64 {
		t.Fatalf("应自算 size/sha256，实际 size=%d sha=%q", out.Files[0].Size, out.Files[0].SHA256)
	}
	bad := baseDesired(FileSpec{Path: "/sf/a.json", SHA256: strings.Repeat("0", 64),
		Source: Source{Kind: SourceInline, Data: []byte("hello")}})
	if _, _, err := Normalize(bad); err == nil {
		t.Fatal("声明摘要与字节不符必须拒绝")
	}
}

// 补丁 4：单文件 > 256KB 且有下载地址 → 自动降级为 package，Data 置空。
func TestNormalizeDowngradesOversizedInlineWithURL(t *testing.T) {
	f := inlineFile("/sf/big.bin", InlineSingleMax+1)
	f.Source.URL = "https://platform/internal/pkg/big"
	out, ev, err := Normalize(baseDesired(f))
	if err != nil {
		t.Fatalf("归一化失败：%v", err)
	}
	if out.Files[0].Source.Kind != SourcePackage || len(out.Files[0].Source.Data) != 0 {
		t.Fatalf("应降级为 package 且清空 Data，实际 %+v", out.Files[0].Source)
	}
	if !hasEvidence(ev, "inline_downgraded_file") {
		t.Fatalf("降级必须写 Evidence：%v", ev)
	}
}

// 无下载地址时不能凭空丢掉要写的内容：保留 inline 并留痕，由硬上限兜住。
func TestNormalizeKeepsInlineWhenNoURLAndRecordsBlock(t *testing.T) {
	out, ev, err := Normalize(baseDesired(inlineFile("/sf/big.bin", InlineSingleMax+1)))
	if err != nil {
		t.Fatalf("归一化失败：%v", err)
	}
	if out.Files[0].Source.Kind != SourceInline {
		t.Fatal("无 URL 时不得把 Data 丢掉")
	}
	if !hasEvidence(ev, "inline_downgrade_blocked") {
		t.Fatalf("降级受阻必须留痕：%v", ev)
	}
}

// 总量 > 1MB：按字节从大到小逐个降级，直到达标，并记 inline_downgraded=n。
// 每份都低于单文件阈值，否则会被单文件规则先降掉，测不到总量规则。
func TestNormalizeDowngradesByTotalUntilUnderLimit(t *testing.T) {
	files := []FileSpec{
		withURL(inlineFile("/sf/a.bin", 250*1024), "https://p/a"),
		withURL(inlineFile("/sf/b.bin", 250*1024), "https://p/b"),
		withURL(inlineFile("/sf/c.bin", 250*1024), "https://p/c"),
		withURL(inlineFile("/sf/d.bin", 250*1024), "https://p/d"),
		withURL(inlineFile("/sf/e.bin", 250*1024), "https://p/e"),
	}
	out, ev, err := Normalize(baseDesired(files...))
	if err != nil {
		t.Fatalf("归一化失败：%v", err)
	}
	total := int64(0)
	for _, f := range out.Files {
		if f.Source.Kind == SourceInline {
			total += f.Size
		}
	}
	if total > InlineTotalMax {
		t.Fatalf("降级后总量仍超限：%d", total)
	}
	if out.Files[0].Source.Kind != SourcePackage {
		t.Fatal("应从最大的文件开始降级")
	}
	if !hasEvidence(ev, "inline_downgraded") {
		t.Fatalf("总量降级必须写 Evidence：%v", ev)
	}
}

// 降级后仍超 10MB → descriptor_too_large，不尝试发送。
func TestNormalizeHardCapFailsClosed(t *testing.T) {
	files := make([]FileSpec, 0, 12)
	for i := 0; i < 12; i++ {
		files = append(files, inlineFile("/sf/f"+string(rune('a'+i))+".bin", 1024*1024))
	}
	_, _, err := Normalize(baseDesired(files...))
	if err == nil || !strings.HasPrefix(err.Error(), CodeDescriptorTooLarge) {
		t.Fatalf("期望 %s，实际 %v", CodeDescriptorTooLarge, err)
	}
}

func withURL(f FileSpec, url string) FileSpec {
	f.Source.URL = url
	return f
}

func hasEvidence(ev []Evidence, key string) bool {
	for _, e := range ev {
		if e.Key == key {
			return true
		}
	}
	return false
}

func TestNormalizeRejectsBadPolicyAndVersionMode(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.json", 8))
	d.Policy.OnFailRollback.Strategy = "rm_rf_everything"
	if _, _, err := Normalize(d); err == nil {
		t.Fatal("非法回滚策略必须拒绝")
	}
	d = baseDesired(inlineFile("/sf/a.json", 8))
	d.Policy.OnSuccessRestore = RestoreSpec{Strategy: RestoreOriginal}
	if _, _, err := Normalize(d); err == nil {
		t.Fatal("restore_original 必须显式声明 WaitFor")
	}
	d = baseDesired(inlineFile("/sf/a.json", 8))
	d.Version.Mode = "whatever"
	if _, _, err := Normalize(d); err == nil {
		t.Fatal("非法版本模式必须拒绝")
	}
}

// DesiredHash 必须稳定：inline 字节不参与序列化，同一份内容两次下发指纹相同。
func TestDesiredHashStableAndContentSensitive(t *testing.T) {
	a, _, err := Normalize(baseDesired(FileSpec{Path: "/sf/a.json", Source: Source{Kind: SourceInline, Data: []byte("x")}}))
	if err != nil {
		t.Fatal(err)
	}
	b, _, err := Normalize(baseDesired(FileSpec{Path: "/sf/a.json", Source: Source{Kind: SourceInline, Data: []byte("x")}}))
	if err != nil {
		t.Fatal(err)
	}
	if DesiredHash(a) != DesiredHash(b) || DesiredHash(a) == "" {
		t.Fatal("同内容指纹必须稳定且非空")
	}
	c, _, _ := Normalize(baseDesired(FileSpec{Path: "/sf/a.json", Source: Source{Kind: SourceInline, Data: []byte("y")}}))
	if DesiredHash(a) == DesiredHash(c) {
		t.Fatal("不同内容指纹必须不同")
	}
}

func TestCompareAlwaysOverwriteForTaskIsolatedKinds(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.json", 8))
	plan := Compare(d, NodeFact{Probed: true, FileExists: true}, &AppliedState{DesiredHash: DesiredHash(d)}, CompareOptions{})
	if plan.Action != ActionInstall || plan.Reason != "always_overwrite" {
		t.Fatalf("config/spc 应总是覆写，实际 %+v", plan)
	}
}

func TestCompareSkipRequiresBothAlignments(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.json", 8))
	d.Version = VersionSpec{Mode: VersionDigest}
	norm, _, err := Normalize(d)
	if err != nil {
		t.Fatal(err)
	}
	aligned := NodeFact{Probed: true, FileExists: true, FileSize: 8, FileDigest: norm.Files[0].SHA256}
	ledger := &AppliedState{DesiredHash: DesiredHash(norm)}

	if plan := Compare(norm, aligned, ledger, CompareOptions{}); plan.Action != ActionSkip {
		t.Fatalf("双对齐应 Skip，实际 %+v", plan)
	}
	// 只有账本对齐、节点被清空 → 必须 Install（B4）。
	wiped := NodeFact{Probed: true, FileExists: false}
	if plan := Compare(norm, wiped, ledger, CompareOptions{}); plan.Action != ActionInstall {
		t.Fatalf("节点被清空后不得 Skip，实际 %+v", plan)
	}
	// 只有节点对齐、账本缺失 → 补一次 Apply 把账本建起来。
	if plan := Compare(norm, aligned, nil, CompareOptions{}); plan.Action != ActionInstall {
		t.Fatalf("账本缺失应补一次 Apply，实际 %+v", plan)
	}
}

func TestCompareProbeFailureDegradedSkip(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.json", 8))
	d.Version = VersionSpec{Mode: VersionDigest}
	norm, _, _ := Normalize(d)
	ledger := &AppliedState{DesiredHash: DesiredHash(norm)}
	plan := Compare(norm, NodeFact{Probed: false}, ledger, CompareOptions{})
	if plan.Action != ActionSkip || !plan.Degraded || plan.Stage != StageProbe {
		t.Fatalf("探测失败降级 Skip 必须带 degraded/stage=probe，实际 %+v", plan)
	}
	plan = Compare(norm, NodeFact{Probed: false}, nil, CompareOptions{})
	if plan.Action != ActionInstall || !plan.Degraded {
		t.Fatalf("无账本时探测失败应 Install 且标降级，实际 %+v", plan)
	}
}

// NodeOnly（acli_sync 无账本链路）：节点事实是唯一真相，
// 期望证据缺失时降级 Skip 而非假"已是最新"（B6）。
func TestCompareNodeOnlySemantics(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.acli", 8))
	d.Kind = KindAcliBinary
	d.Version = VersionSpec{Mode: VersionOrdered, Expected: "1.0.3"}
	norm, _, _ := Normalize(d)
	opts := CompareOptions{NodeOnly: true, CompareVersion: func(fact NodeFact, want VersionSpec) int {
		if fact.Version == want.Expected {
			return 0
		}
		return -1
	}}

	// 节点对齐 + 无账本：直接 Skip，不为建账本补 Apply（否则 acli 每次登录都重装）。
	plan := Compare(norm, NodeFact{Probed: true, Version: "1.0.3"}, nil, opts)
	if plan.Action != ActionSkip || plan.Reason != "up_to_date" || plan.Degraded {
		t.Fatalf("NodeOnly 节点对齐应无降级 Skip，实际 %+v", plan)
	}

	// 期望证据缺失（Probed=false）+ 已安装：降级 Skip，绝不回"已是最新"。
	plan = Compare(norm, NodeFact{Probed: false, Version: "1.0.0"}, nil, opts)
	if plan.Action != ActionSkip || !plan.Degraded || plan.Reason != "probe_failed_no_desired" || plan.Stage != StageProbe {
		t.Fatalf("期望缺失应降级 Skip 并留证，实际 %+v", plan)
	}

	// 期望缺失但明确未安装：仍须 Install（不能因源站故障把"没装"也跳过）。
	plan = Compare(norm, NodeFact{Probed: false}, nil, opts)
	if plan.Action != ActionInstall || plan.Reason != "not_installed" || !plan.Degraded {
		t.Fatalf("未安装不受期望缺失影响，实际 %+v", plan)
	}

	// force 先行：期望缺失也拦不住用户显式意图。
	forceOpts := opts
	forceOpts.Force = true
	if plan = Compare(norm, NodeFact{Probed: false, Version: "1.0.0"}, nil, forceOpts); plan.Action != ActionInstall {
		t.Fatalf("force 应无条件 Install，实际 %+v", plan)
	}

	// 节点落后：照常 Install（NodeOnly 不影响差量判定）。
	if plan = Compare(norm, NodeFact{Probed: true, Version: "1.0.0"}, nil, opts); plan.Action != ActionInstall || plan.Reason != "version_outdated" {
		t.Fatalf("节点落后应 Install，实际 %+v", plan)
	}
}

func TestCompareOrderedVersionAndDowngradeBlock(t *testing.T) {
	d := baseDesired(inlineFile("/sf/a.acli", 8))
	d.Kind = KindAcliBinary
	d.Version = VersionSpec{Mode: VersionOrdered, Expected: "1.0.3", Build: "2026-11-01"}
	norm, _, _ := Normalize(d)
	ledger := &AppliedState{DesiredHash: DesiredHash(norm)}
	opts := CompareOptions{CompareVersion: func(fact NodeFact, want VersionSpec) int {
		if fact.Version == want.Expected {
			return 0
		}
		if fact.Version > want.Expected {
			return 1
		}
		return -1
	}}
	fact := NodeFact{Probed: true, Version: "1.0.3"}
	if plan := Compare(norm, fact, ledger, opts); plan.Action != ActionSkip {
		t.Fatalf("版本相等且双对齐应 Skip，实际 %+v", plan)
	}
	fact.Version = "1.0.0"
	if plan := Compare(norm, fact, ledger, opts); plan.Action != ActionInstall || plan.Reason != "version_outdated" {
		t.Fatalf("节点更旧应 Install，实际 %+v", plan)
	}
	fact.Version = "1.0.9"
	if plan := Compare(norm, fact, ledger, opts); plan.Action != ActionBlockedDowngrade {
		t.Fatalf("节点更新必须禁降级，实际 %+v", plan)
	}
	// force 覆盖禁降级：用户显式意图优先。
	if plan := Compare(norm, fact, ledger, CompareOptions{Force: true, CompareVersion: opts.CompareVersion}); plan.Action != ActionInstall {
		t.Fatalf("force 应无条件 Install，实际 %+v", plan)
	}
	// 未安装。
	if plan := Compare(norm, NodeFact{Probed: true}, ledger, opts); plan.Reason != "not_installed" {
		t.Fatalf("未安装应判 not_installed，实际 %+v", plan)
	}
}

func TestCompareHashThreeStates(t *testing.T) {
	d := DesiredState{Kind: KindPluginBundle, NodeIP: "172.28.24.1",
		Files:   []FileSpec{{Path: "/sf/plugins/p.tar.gz", Size: 4, Source: Source{Kind: SourcePackage, URL: "https://p/pkg"}}},
		Version: VersionSpec{Mode: VersionHash, Expected: "spec-abc"}}
	norm, _, err := Normalize(d)
	if err != nil {
		t.Fatal(err)
	}
	ledger := &AppliedState{DesiredHash: DesiredHash(norm)}
	same := NodeFact{Probed: true, FileExists: true, FileDigest: "spec-abc"}
	if plan := Compare(norm, same, ledger, CompareOptions{}); plan.Action != ActionSkip {
		t.Fatalf("无修改应 Skip，实际 %+v", plan)
	}
	changed := NodeFact{Probed: true, FileExists: true, FileDigest: "spec-old"}
	if plan := Compare(norm, changed, ledger, CompareOptions{}); plan.Action != ActionInstall {
		t.Fatalf("有修改应 Install，实际 %+v", plan)
	}
	absent := NodeFact{Probed: true, FileExists: false}
	if plan := Compare(norm, absent, ledger, CompareOptions{}); plan.Action != ActionInstall || plan.Reason != "plugin_absent" {
		t.Fatalf("新增应 Install，实际 %+v", plan)
	}
}

func TestStoreRoundTripAndAtomicWrite(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(dir)
	if st, err := store.Load(KindConfigFile, "172.28.24.1"); err != nil || st != nil {
		t.Fatalf("不存在应返回 (nil,nil)，实际 %v / %v", st, err)
	}
	want := &AppliedState{Kind: KindConfigFile, NodeIP: "172.28.24.1",
		DesiredHash: "abc", AppliedAt: time.Now().UTC().Truncate(time.Second),
		Evidence: []Evidence{KV("integrity", "sha256")}}
	if err := store.Save(want); err != nil {
		t.Fatalf("落盘失败：%v", err)
	}
	got, err := store.Load(want.Kind, want.NodeIP)
	if err != nil || got == nil {
		t.Fatalf("读回失败：%v / %v", got, err)
	}
	if got.DesiredHash != want.DesiredHash || !got.AppliedAt.Equal(want.AppliedAt) {
		t.Fatalf("读回内容不符：%+v", got)
	}
	// 临时文件 + rename：目录里不得留下 .tmp 残渣。
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Fatalf("临时文件残留：%s", e.Name())
		}
	}
	raw, _ := os.ReadFile(filepath.Join(dir, stateFileName(want.Kind, want.NodeIP)))
	var check map[string]any
	if err := json.Unmarshal(raw, &check); err != nil {
		t.Fatalf("落盘必须是完整 JSON：%v", err)
	}
	list, err := store.List()
	if err != nil || len(list) != 1 {
		t.Fatalf("List 应返回 1 条，实际 %d / %v", len(list), err)
	}
	if err := store.Delete(want.Kind, want.NodeIP); err != nil {
		t.Fatalf("删除失败：%v", err)
	}
	if st, _ := store.Load(want.Kind, want.NodeIP); st != nil {
		t.Fatal("删除后仍能读到")
	}
}

// 账本损坏不得被当成"没记录"静默重写：那会抹掉"上次同步了什么"。
func TestStoreCorruptLedgerReturnsError(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(dir)
	if err := os.WriteFile(filepath.Join(dir, stateFileName(KindSpcSlot, "172.28.24.1")), []byte("{half"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(KindSpcSlot, "172.28.24.1"); err == nil {
		t.Fatal("损坏账本必须报错")
	}
}

// 桌面模式账本目录由 HCI_BRIDGE_LOG_DIR 同源决定，不写死 HOME。
func TestDefaultStateDirFollowsLogDirEnv(t *testing.T) {
	t.Setenv("HCI_BRIDGE_LOG_DIR", "/tmp/bridge-logs")
	if got := DefaultStateDir(); got != filepath.Join("/tmp/bridge-logs", "reconcile", "state") {
		t.Fatalf("桌面模式目录应同源，实际 %q", got)
	}
	t.Setenv("HCI_BRIDGE_LOG_DIR", "")
	if got := DefaultStateDir(); got != "/var/lib/terminal-bridge/reconcile/state" {
		t.Fatalf("cluster 模式应落 PVC，实际 %q", got)
	}
}

func TestInlineMessageRequiresSecondValidation(t *testing.T) {
	p := InlineMessage{}
	if _, err := p.Desired(context.Background(), SyncRequest{Kind: KindConfigFile, NodeIP: "1.2.3.4", Raw: "{}", Path: "/sf/a.json"}); err == nil {
		t.Fatal("没有二次校验不得写节点（前端是不可信链路）")
	}
	p.Validate = func(string) error { return nil }
	if _, err := p.Desired(context.Background(), SyncRequest{Kind: KindConfigFile, NodeIP: "1.2.3.4", Path: "/sf/a.json"}); err == nil {
		t.Fatal("空原文必须拒绝")
	}
	if _, err := p.Desired(context.Background(), SyncRequest{Kind: KindConfigFile, NodeIP: "1.2.3.4", Raw: "{}", Path: "relative.json"}); err == nil {
		t.Fatal("非法路径必须拒绝")
	}
	if _, err := p.Desired(context.Background(), SyncRequest{Kind: KindConfigFile, NodeIP: "1.2.3.4", Raw: "{}", Path: "/sf/a.json", SHA256: strings.Repeat("0", 64)}); err == nil {
		t.Fatal("摘要与平台签发值不符必须拒绝")
	}
	list, err := p.Desired(context.Background(), SyncRequest{Kind: KindConfigFile, NodeIP: "1.2.3.4", Raw: "{}", Path: "/sf/a.json"})
	if err != nil || len(list) != 1 {
		t.Fatalf("合法请求应产出 1 条期望态：%v / %d", err, len(list))
	}
	if list[0].Smoke.Kind != SmokeSHA256 || list[0].Install.Kind != InstallNone {
		t.Fatalf("config 默认冒烟应为 sha256、无安装动作，实际 %+v", list[0])
	}
}

// 回执降级必须可判：调用方据此决定前端能不能显示"已是最新"。
func TestReceiptDegradedAndEvidence(t *testing.T) {
	r := Skipped(StageProbe, "probe_failed_ledger_aligned", true, KV("ledger_aligned", "true"))
	if !r.Degraded || r.Field("ledger_aligned") != "true" || r.Field("reason") == "" {
		t.Fatalf("降级回执证据不完整：%s", r.Summary())
	}
	r2 := r.WithEvidence(KV("integrity", "md5"))
	if len(r.Evidence) != 2 || len(r2.Evidence) != 3 {
		t.Fatal("WithEvidence 不得修改接收者")
	}
	if !strings.Contains(r2.Summary(), "degraded=true") {
		t.Fatalf("摘要应含降级标记：%s", r2.Summary())
	}
}
