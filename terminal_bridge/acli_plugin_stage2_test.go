package main

// acli_sync 第二阶段（插件差量同步）单测。
//
// 覆盖的是收口设计 §3.3 的失败语义与 S4c 验收里能在单测层证明的部分：
//   - 差量三态：新增 / 有修改 / 无修改（验收 7）；
//   - 节点被清空后凭事实重装，不 Skip（B4，验收 8）；
//   - 无备份不删的构造性不变量（B1，验收 10 的脚本侧前提）；
//   - 清单二次校验 fail-closed（验收 9 的桥侧半边）；
//   - 旧前端不带凭据 → 阶段二整条路径不执行（验收 11）。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"terminal_bridge/internal/shared/models"
	"testing"
	"time"
)

// newTestPluginDesired 造一份**必然合规**的期望态：摘要按内容现算，
// 这样每个用例只改它要证明的那一处，不会被无关的摘要校验先拒掉。
func newTestPluginDesired() acliPluginDesired {
	files := []acliPluginFile{
		{Path: "demo/plugin", Mode: "0755", Content: "#!/bin/bash\necho demo\n"},
		{Path: "demo/lib/helper.sh", Mode: "0644", Content: "echo helper\n"},
	}
	for index := range files {
		files[index].SHA256 = sha256Hex([]byte(files[index].Content))
	}
	return acliPluginDesired{
		Name:      "demo",
		Version:   "1.0.0",
		SpecHash:  strings.Repeat("a", 64),
		SmokeArgs: "--selftest",
		Size:      int64(len(files[0].Content) + len(files[1].Content)),
		Files:     files,
		RegisterItem: map[string]any{
			"id": "demo", "name": "demo", "version": "1.0.0",
			"status": "enabled", "source": "platform",
		},
	}
}

// manifestJSON 把期望态包成平台响应体（content 内联，与 case-service 同构）。
func manifestJSON(t *testing.T, items ...acliPluginDesired) []byte {
	t.Helper()
	payload := map[string]any{"manifest_version": "2.0", "case_id": "Q2026111800001"}
	plugins := make([]map[string]any, 0, len(items))
	for _, item := range items {
		plugins = append(plugins, map[string]any{
			"name": item.Name, "version": item.Version, "spec_hash": item.SpecHash,
			"update_mode": item.UpdateMode, "built_in": item.BuiltIn,
			"smoke_args": item.SmokeArgs, "enable_completion": item.EnableCompletion,
			"size": item.Size, "package_ref": item.PackageRef, "sha256": item.SHA256,
			"files": item.Files, "register_item": item.RegisterItem,
		})
	}
	payload["plugins"] = plugins
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("构造清单失败: %v", err)
	}
	return body
}

func TestParseAcliPluginManifestAcceptsValid(t *testing.T) {
	desired := newTestPluginDesired()
	plugins, err := parseAcliPluginManifest(manifestJSON(t, desired), "Q2026111800001")
	if err != nil {
		t.Fatalf("合规清单应当被接受: %v", err)
	}
	if len(plugins.Plugins) != 1 || plugins.Plugins[0].Name != "demo" {
		t.Fatalf("插件解析结果不对: %+v", plugins.Plugins)
	}
	if plugins.ManifestVersion != "2.0" {
		t.Fatalf("manifest_version 应当透传，实际 %q", plugins.ManifestVersion)
	}
}

func TestParseAcliPluginManifestRejectsUntrusted(t *testing.T) {
	cases := []struct {
		name     string
		mutate   func(*acliPluginDesired)
		wantCode string
	}{
		{"插件名注入", func(d *acliPluginDesired) { d.Name = "demo; rm -rf /" }, "manifest_invalid_plugin_name"},
		{"spec_hash 非十六进制", func(d *acliPluginDesired) { d.SpecHash = "zzzz" }, "manifest_invalid_spec_hash"},
		{"冒烟参数带命令替换", func(d *acliPluginDesired) { d.SmokeArgs = "--x $(id)" }, "manifest_invalid_smoke_args"},
		{"文件路径越界", func(d *acliPluginDesired) { d.Files[0].Path = "../../etc/passwd" }, "manifest_invalid_file_path"},
		{"文件路径不以插件名开头", func(d *acliPluginDesired) { d.Files[0].Path = "other/plugin" }, "manifest_invalid_file_path"},
		{"权限不合规", func(d *acliPluginDesired) { d.Files[1].Mode = "75" }, "manifest_invalid_mode"},
		{"文件摘要不合规", func(d *acliPluginDesired) { d.Files[1].SHA256 = "deadbeef" }, "manifest_invalid_file_digest"},
		{"内容与摘要不符", func(d *acliPluginDesired) { d.Files[1].Content = "tampered\n" }, "manifest_file_digest_mismatch"},
		{"入口脚本不可执行", func(d *acliPluginDesired) {
			d.Files[0].Mode = "0644"
			d.Files[0].SHA256 = sha256Hex([]byte(d.Files[0].Content))
		}, "manifest_invalid_wrapper_mode"},
		{"单文件超限", func(d *acliPluginDesired) {
			content := strings.Repeat("x", acliPluginMaxFileBytes+1)
			d.Files[1].Content = content
			d.Files[1].SHA256 = sha256Hex([]byte(content))
			d.Size = 0
		}, "manifest_file_too_large"},
		{"注册项 name 不一致", func(d *acliPluginDesired) { d.RegisterItem["name"] = "evil" }, "manifest_register_name_mismatch"},
		{"缺注册项", func(d *acliPluginDesired) { d.RegisterItem = nil }, "manifest_missing_register_item"},
		{"只有包没有文件", func(d *acliPluginDesired) {
			d.Files = nil
			d.PackageRef = "demo.tar"
			d.SHA256 = strings.Repeat("b", 64)
		}, "manifest_package_channel_unsupported"},
		{"有包但无摘要", func(d *acliPluginDesired) { d.PackageRef = "demo.tar" }, "manifest_package_without_digest"},
		{"文件清单为空", func(d *acliPluginDesired) { d.Files = nil }, "manifest_empty_files"},
	}
	for _, item := range cases {
		desired := newTestPluginDesired()
		item.mutate(&desired)
		_, err := parseAcliPluginManifest(manifestJSON(t, desired), "Q2026111800001")
		if err == nil {
			t.Fatalf("%s：应当拒收，却通过了", item.name)
		}
		if got := acliPluginErrorCode(err); got != item.wantCode {
			t.Fatalf("%s：原因码应为 %s，实际 %s（%v）", item.name, item.wantCode, got, err)
		}
	}
}

func TestParseAcliPluginManifestRejectsBadJSONAndCaseMismatch(t *testing.T) {
	if _, err := parseAcliPluginManifest([]byte("{not json"), "Q2026111800001"); err == nil ||
		acliPluginErrorCode(err) != "manifest_invalid_json" {
		t.Fatalf("非法 JSON 必须拒收，实际 %v", err)
	}
	// 拿错工单：票据只绑一个 case，case_id 不一致就是别人的期望态。
	if _, err := parseAcliPluginManifest(manifestJSON(t, newTestPluginDesired()), "Q2026111800002"); err == nil ||
		acliPluginErrorCode(err) != "manifest_case_mismatch" {
		t.Fatalf("工单不一致必须拒收，实际 %v", err)
	}
}

func TestParseAcliPluginManifestRejectsDuplicatePlugin(t *testing.T) {
	first := newTestPluginDesired()
	second := newTestPluginDesired()
	second.SpecHash = strings.Repeat("c", 64)
	if _, err := parseAcliPluginManifest(manifestJSON(t, first, second), "Q2026111800001"); err == nil ||
		acliPluginErrorCode(err) != "manifest_duplicate_plugin" {
		t.Fatalf("同名插件必须整单拒收，实际 %v", err)
	}
}

func TestValidatePluginPlatformURL(t *testing.T) {
	cases := []struct {
		raw     string
		wantErr string
	}{
		{"https://hci.example.com/", ""},
		{"http://127.0.0.1:8000", ""},                       // 本地联调：回环允许 http
		{"http://localhost:8000", ""},                       // 同上，按主机名判定
		{"http://hci.example.com", "platform_url_insecure"}, // 公网明文一律拒绝
		{"ftp://hci.example", "platform_url_invalid"},
		{"not a url", "platform_url_invalid"},
		{"", "platform_url_missing"},
	}
	for _, item := range cases {
		_, err := validatePluginPlatformURL(item.raw)
		if item.wantErr == "" {
			if err != nil {
				t.Fatalf("%q 应当放行，实际 %v", item.raw, err)
			}
			continue
		}
		if err == nil || acliPluginErrorCode(err) != item.wantErr {
			t.Fatalf("%q 应当回 %s，实际 %v", item.raw, item.wantErr, err)
		}
	}
}

// ── 差量三态（验收 7 / 8）──────────────────────────────────────────────────────

func TestCompareAcliPluginStateThreeWays(t *testing.T) {
	desired := newTestPluginDesired()
	inSync := acliPluginHostState{
		IP:               "10.0.0.1",
		DigestComparable: true,
		Register:         map[string]any{"id": "demo", "name": "demo", "version": "1.0.0", "status": "enabled", "source": "platform"},
		Files: []acliPluginFileState{
			{Path: "plugin", State: "ok", Digest: desired.Files[0].SHA256},
			{Path: "lib/helper.sh", State: "ok", Digest: desired.Files[1].SHA256},
		},
	}

	// 无修改 → Skip
	if action, reason := compareAcliPluginState(desired, inSync); action != acliPluginActionSkip || reason != "in_sync" {
		t.Fatalf("注册项与每个文件摘要都对得上时应 Skip，实际 %s/%s", action, reason)
	}

	// 新增：注册项不存在
	fresh := inSync
	fresh.Register = nil
	fresh.Files = nil
	if action, _ := compareAcliPluginState(desired, fresh); action != acliPluginActionInstall {
		t.Fatalf("注册项缺失应判新增，实际 %s", action)
	}

	// 有修改：任一文件摘要对不上
	changed := inSync
	changed.Files = []acliPluginFileState{
		{Path: "plugin", State: "ok", Digest: desired.Files[0].SHA256},
		{Path: "lib/helper.sh", State: "ok", Digest: strings.Repeat("f", 64)},
	}
	if action, reason := compareAcliPluginState(desired, changed); action != acliPluginActionOverwrite ||
		!strings.HasPrefix(reason, "file_digest_mismatch") {
		t.Fatalf("摘要不一致应判有修改，实际 %s/%s", action, reason)
	}

	// 有修改：注册项字段变了（version 被第三方改小）
	regChanged := inSync
	regChanged.Register = map[string]any{"id": "demo", "name": "demo", "version": "0.9.0", "status": "enabled", "source": "platform"}
	if action, reason := compareAcliPluginState(desired, regChanged); action != acliPluginActionOverwrite ||
		!strings.HasPrefix(reason, "register_changed") {
		t.Fatalf("注册项不一致应判有修改，实际 %s/%s", action, reason)
	}
}

// TestCompareAcliPluginStateClearedDirectory 是 B4 的根治用例：
// acli 重装清掉插件目录，但注册项还在——只看注册表就会判出"无需修改"。
func TestCompareAcliPluginStateClearedDirectory(t *testing.T) {
	desired := newTestPluginDesired()
	cleared := acliPluginHostState{
		IP:               "10.0.0.1",
		DigestComparable: true,
		Register:         map[string]any{"id": "demo", "name": "demo", "version": "1.0.0", "status": "enabled", "source": "platform"},
		Files: []acliPluginFileState{
			{Path: "plugin", State: "missing"},
			{Path: "lib/helper.sh", State: "missing"},
		},
	}
	if action, reason := compareAcliPluginState(desired, cleared); action != acliPluginActionOverwrite ||
		!strings.HasPrefix(reason, "file_missing") {
		t.Fatalf("目录被清空必须重装（不 Skip），实际 %s/%s", action, reason)
	}
}

// TestCompareAcliPluginStateNeverSkipsOnUnprobed 是 B6 同族约束：
// "读不到"永远不能被洗成"已是最新"。
func TestCompareAcliPluginStateNeverSkipsOnUnprobed(t *testing.T) {
	desired := newTestPluginDesired()
	inSync := acliPluginHostState{
		DigestComparable: true,
		Register:         map[string]any{"name": "demo", "version": "1.0.0"},
		Files: []acliPluginFileState{
			{Path: "plugin", State: "ok", Digest: desired.Files[0].SHA256},
			{Path: "lib/helper.sh", State: "ok", Digest: desired.Files[1].SHA256},
		},
	}
	degraded := []acliPluginHostState{
		{ProbeErr: "exit=255"},    // 探测失败
		{DigestComparable: false}, // 缺 jq / 缺 sha256sum
		{Register: inSync.Register, Files: []acliPluginFileState{{Path: "plugin", State: "ok", Digest: desired.Files[0].SHA256}}}, // 少探一个文件
		{Register: inSync.Register, Files: []acliPluginFileState{ // 读失败
			{Path: "plugin", State: "read_failed"}, {Path: "lib/helper.sh", State: "ok", Digest: desired.Files[1].SHA256},
		}},
	}
	for index, state := range degraded {
		action, reason := compareAcliPluginState(desired, state)
		if action == acliPluginActionSkip {
			t.Fatalf("降级样本 %d 不得判 Skip：%s", index, reason)
		}
		if action != acliPluginActionOverwrite {
			t.Fatalf("降级样本 %d 应按有修改收敛，实际 %s/%s", index, action, reason)
		}
	}
}

// ── 探测脚本与探测解析 ────────────────────────────────────────────────────────

func TestBuildAcliPluginProbeCommandReadsBothFacts(t *testing.T) {
	desired := newTestPluginDesired()
	command := buildAcliPluginProbeCommand(desired)
	for _, want := range []string{
		"plugin_register.json", // 注册表事实
		"$BASE/$NAME/plugin",   // 磁盘文件事实：只看注册表会重演 B4
		"sha256sum",            // 逐文件摘要
		acpStateBeginMark, acpStateEndMark,
		acliPluginRegNoJQ, acliPluginRegBad, // MISSING 与"读不到"必须可区分
	} {
		if !strings.Contains(command, want) {
			t.Fatalf("探测脚本缺少 %q：%s", want, command)
		}
	}
}

func TestParseAcliPluginProbeOutput(t *testing.T) {
	desired := newTestPluginDesired()
	stdout := acpStateBeginMark + "\n" +
		"REG\t" + `{"id":"demo","name":"demo","version":"1.0.0"}` + "\n" +
		"F\tplugin\tok\t" + desired.Files[0].SHA256 + "\n" +
		"F\tlib/helper.sh\tmissing\n" +
		acpStateEndMark
	state := parseAcliPluginProbeOutput("10.0.0.1", stdout, 0, nil)
	if state.ProbeErr != "" || !state.DigestComparable {
		t.Fatalf("正常探测不应记错：%+v", state)
	}
	if state.Register["name"] != "demo" {
		t.Fatalf("注册项未解析：%+v", state.Register)
	}
	if len(state.Files) != 2 || state.Files[1].State != "missing" {
		t.Fatalf("逐文件状态解析不对：%+v", state.Files)
	}

	// 注册表不存在/条目不存在：合法事实，不记错（判新增）。
	for _, marker := range []string{acliPluginRegNoFile, acliPluginRegMissing} {
		ok := parseAcliPluginProbeOutput("10.0.0.1", acpStateBeginMark+"\nREG\t"+marker+"\n"+acpStateEndMark, 0, nil)
		if ok.ProbeErr != "" {
			t.Fatalf("%s 是合法事实，不应记错：%+v", marker, ok)
		}
	}
	// 注册表损坏：必须留痕，不能当成"没装过"。
	bad := parseAcliPluginProbeOutput("10.0.0.1", acpStateBeginMark+"\nREG\t"+acliPluginRegBad+"\n"+acpStateEndMark, 0, nil)
	if bad.ProbeErr != "register_read_failed" {
		t.Fatalf("BAD 注册表必须留痕：%+v", bad)
	}
	// 缺 jq：等价于判定不了，不可比。
	noJQ := parseAcliPluginProbeOutput("10.0.0.1", acpStateBeginMark+"\nREG\t"+acliPluginRegNoJQ+"\n"+acpStateEndMark, 0, nil)
	if noJQ.DigestComparable {
		t.Fatalf("缺 jq 时必须标记不可比：%+v", noJQ)
	}
	// 没有标记块：探测失败，绝不回"一致"。
	empty := parseAcliPluginProbeOutput("10.0.0.1", "bash: jq: not found", 0, nil)
	if empty.ProbeErr != "state_block_missing" || empty.DigestComparable {
		t.Fatalf("缺状态块应判探测失败：%+v", empty)
	}
	// 非零退出：直接不可比。
	failed := parseAcliPluginProbeOutput("10.0.0.1", "", 255, nil)
	if failed.DigestComparable {
		t.Fatalf("非零退出应不可比：%+v", failed)
	}
}

// TestProbeCommandRealShellRoundTrip 把生成的探测脚本交给**真实本地 shell** 跑一遍，
// 再把输出喂回解析端：脚本与解析的契约必须由真实执行证明，手工拼的 stdout 暴露不了
// “脚本多打一遍 REG 前缀”这类缺陷（2026-11-19 端到端实测：恒判
// register_json_unparsable → 永远覆盖、永不跳过）。
func TestProbeCommandRealShellRoundTrip(t *testing.T) {
	for _, tool := range []string{"jq", "sha256sum"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("本地缺 %s，无法跑真实探测脚本", tool)
		}
	}
	desired := newTestPluginDesired()
	base := t.TempDir()
	regPath := filepath.Join(base, "plugin_register.json")
	// 铺“已收敛”事实：注册项与逐文件内容都与期望态一致。
	regItem, err := json.Marshal(desired.RegisterItem)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(regPath, []byte("["+string(regItem)+"]"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, file := range desired.Files {
		rel := strings.TrimPrefix(file.Path, desired.Name+"/")
		p := filepath.Join(base, desired.Name, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(file.Content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// 只把硬编码的节点路径换成夹具目录，脚本其余部分逐字不动。
	command := buildAcliPluginProbeCommand(desired)
	command = strings.Replace(command, "BASE="+shellQuote(acliPluginBaseDir), "BASE="+shellQuote(base), 1)
	command = strings.Replace(command, "REG="+shellQuote(acliPluginRegisterPath), "REG="+shellQuote(regPath), 1)

	runProbe := func() acliPluginHostState {
		t.Helper()
		var stdout, stderr bytes.Buffer
		cmd := exec.Command("/bin/sh", "-c", command)
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		runErr := cmd.Run()
		code := 0
		if exitErr, ok := runErr.(*exec.ExitError); ok {
			code = exitErr.ExitCode()
		} else if runErr != nil {
			t.Fatalf("探测脚本无法执行：%v", runErr)
		}
		return parseAcliPluginProbeOutput("127.0.0.1", stdout.String(), code, runErr)
	}

	// 已收敛：注册项可解析 + 逐文件摘要一致 → 必须能判 Skip。
	state := runProbe()
	if state.ProbeErr != "" || !state.DigestComparable {
		t.Fatalf("已收敛事实不应记错：%+v", state)
	}
	if action, reason := compareAcliPluginState(desired, state); action != acliPluginActionSkip {
		t.Fatalf("已收敛应判 Skip，实际 %s/%s（state=%+v）", action, reason, state)
	}

	// 注册项被删：凭事实判新增，而不是探测失败。
	if err := os.WriteFile(regPath, []byte("[]"), 0o644); err != nil {
		t.Fatal(err)
	}
	state = runProbe()
	if state.ProbeErr != "" {
		t.Fatalf("空注册表是合法事实，不应记错：%+v", state)
	}
	if action, reason := compareAcliPluginState(desired, state); action != acliPluginActionInstall || reason != "register_entry_missing" {
		t.Fatalf("注册项缺失应判新增，实际 %s/%s", action, reason)
	}
}

// ── 设备侧脚本不变量（B1 无备份不删）──────────────────────────────────────────

func TestRenderAcliPluginDeployScriptInvariants(t *testing.T) {
	desired := newTestPluginDesired()
	script, err := renderAcliPluginDeployScript(desired, "htp-plugin-demo-abc-1")
	if err != nil {
		t.Fatalf("渲染失败: %v", err)
	}
	for _, want := range []string{
		`flock -w 30 9`,  // 与 acli plugin install/uninstall/sync 共用同一把锁
		`printf '%b'`,    // HCI 无 base64/python3：写盘只能走 printf
		`sha256sum -c -`, // 逐文件摘要复核
		`cp -a "$REG" "$TMP/backup/plugin_register.json"`, // 先备份再动手
		`mv "$TMP/plugin_register.json" "$REG"`,           // jq 写临时文件后原子替换
		acpResultBeginMark, acpSmokeBegin,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("部署脚本缺少必需片段 %q：\n%s", want, script)
		}
	}
	// B1 构造性不变量：目录删除必须以「有备份 ∨ 有 .old」为前提。
	if !strings.Contains(script, `if [ -d "$TMP/backup/$NAME" ] || [ -d "$BASE/$NAME.old.$DEPLOY_ID" ]; then`) {
		t.Fatalf("rollback 的删目录分支必须受备份前提保护（B1）：\n%s", script)
	}
	if !strings.Contains(script, acpRollbackSkipMark+"no_backup") {
		t.Fatalf("无备份时必须显式打出跳过标记并留痕：\n%s", script)
	}
	// 备份之前的失败点只能 fail，不能碰目录。
	for _, early := range []string{"lock_timeout", "register_backup_failed"} {
		if !strings.Contains(script, `"`+early+`"`) && !strings.Contains(script, `'`+early+`'`) {
			t.Fatalf("早期失败点 %s 必须走 fail（此时尚无备份）：\n%s", early, script)
		}
	}
	// 冒烟输出为空不算成功。
	if !strings.Contains(script, `[ -s "$TMP/smoke.out" ]`) {
		t.Fatalf("必须有\"冒烟输出非空\"这道判据：\n%s", script)
	}
}

func TestRenderAcliPluginDeployScriptBuiltInKeepsVendorFiles(t *testing.T) {
	desired := newTestPluginDesired()
	desired.BuiltIn = true
	desired.Name = "log_collect"
	desired.RegisterItem["name"] = "log_collect"
	for index := range desired.Files {
		desired.Files[index].Path = strings.Replace(desired.Files[index].Path, "demo/", "log_collect/", 1)
	}
	script, err := renderAcliPluginDeployScript(desired, "htp-plugin-log_collect-abc-1")
	if err != nil {
		t.Fatalf("渲染失败: %v", err)
	}
	// 内置插件：整树复制后只覆盖规格文件，并由设备侧重算 manifest.hash。
	for _, want := range []string{`cp -a "$BASE/$NAME/." "$TMP/stage/$NAME/"`, `.hash = $h`} {
		if !strings.Contains(script, want) {
			t.Fatalf("内置插件脚本缺少厂商共存片段 %q：\n%s", want, script)
		}
	}
	// 入口脚本保留厂商原件，不重写（只允许 chmod，不允许 printf 重定向）。
	if strings.Contains(script, `> "$TMP/stage/$NAME/plugin"`) {
		t.Fatalf("内置插件不应重写入口脚本：\n%s", script)
	}
}

// ── 设备侧自检解析 ────────────────────────────────────────────────────────────

func TestParseAcliPluginDeployResult(t *testing.T) {
	success := func(status, smoke string) string {
		out := acpResultBeginMark + "\n" +
			`{"status":"` + status + `","plugin_name":"demo","version":"1.0.0","spec_hash":"` + strings.Repeat("a", 64) + `","deploy_id":"d1","error":""}` + "\n" +
			acpResultEndMark
		if smoke != "" {
			out += "\n" + acpSmokeBegin + "\n" + smoke + "\n" + acpSmokeEnd
		}
		return out
	}
	if got := parseAcliPluginDeployResult(success("success", "selftest ok"), 0); !got.OK || got.Status != "success" {
		t.Fatalf("success + 退出码 0 + 冒烟非空才算生效：%+v", got)
	}
	// 脚本任何分支都以 0 结束：退出码 0 本身不构成结论。
	if got := parseAcliPluginDeployResult(success("rolled_back", ""), 0); got.OK || got.Status != "rolled_back" {
		t.Fatalf("回滚不得当成功：%+v", got)
	}
	if got := parseAcliPluginDeployResult(success("success", ""), 0); got.OK {
		t.Fatalf("冒烟输出为空不得算成功：%+v", got)
	}
	missing := parseAcliPluginDeployResult("bash: jq: not found", 0)
	if missing.OK || missing.Error != "device_result_missing" {
		t.Fatalf("没有自检块必须如实报\"没结论\"：%+v", missing)
	}
	if got := parseAcliPluginDeployResult(success("success", "ok"), 1); got.OK {
		t.Fatalf("非零退出不得算成功：%+v", got)
	}
}

// ── 回执与编排（验收 10 / 11）─────────────────────────────────────────────────

func TestAcliPluginActionRankFailsWorst(t *testing.T) {
	if acliPluginActionRank(acliPluginActionFailed) <= acliPluginActionRank(acliPluginActionOverwrite) ||
		acliPluginActionRank(acliPluginActionOverwrite) <= acliPluginActionRank(acliPluginActionInstall) ||
		acliPluginActionRank(acliPluginActionInstall) <= acliPluginActionRank(acliPluginActionSkip) {
		t.Fatalf("动作优先级必须是 failed > overwrite > install > skip")
	}
}

func TestAcliPluginStageDisabledForOldFrontend(t *testing.T) {
	// 旧前端不带 platform_url / plugin_ticket：阶段二整条路径不执行，也不发回执。
	if acliPluginStageRequested(models.InMessage{}) {
		t.Fatalf("无凭据时不应启用阶段二")
	}
	if acliPluginStageRequested(models.InMessage{PlatformURL: "https://hci.example"}) {
		t.Fatalf("只有 platform_url 不应启用阶段二")
	}
	if acliPluginStageRequested(models.InMessage{PluginTicket: "t.1"}) {
		t.Fatalf("只有票据不应启用阶段二")
	}
	if !acliPluginStageRequested(models.InMessage{PlatformURL: "https://hci.example", PluginTicket: "t.1"}) {
		t.Fatalf("两个字段齐备时应启用阶段二")
	}
	// 未启用时连“拉期望态”这一步都不发生（不发 HTTP、不留 plan），回执也不得产出。
	plan := (&Bridge{}).prepareAcliPluginStage(models.InMessage{}, "", "")
	if plan != nil {
		t.Fatalf("无凭据时不应有阶段二计划：%+v", plan)
	}
	if receipt := (&Bridge{}).runAcliPluginStage(nil, models.InMessage{}, plan, "", ""); receipt != nil {
		t.Fatalf("未启用时不应产出回执：%+v", receipt)
	}
}

func TestAcliPluginStageReceiptMissingTicketStillReported(t *testing.T) {
	// 只有 platform_url 时，阶段二未被启用（不发回执）；带齐两字段但票据为空则由
	// fetch 侧回 plugin_ticket_missing——这里直接校验错误码语义。
	if got := acliPluginErrorCode(newAcliPluginError("plugin_ticket_missing", "")); got != "plugin_ticket_missing" {
		t.Fatalf("结构化原因码必须原样取出，实际 %s", got)
	}
	if got := acliPluginErrorCode(errors.New("boom")); got != "stage2_unknown" {
		t.Fatalf("非结构化错误必须归到 stage2_unknown，实际 %s", got)
	}
}

func TestAcliPluginStageFetchFailureKeepsReasonCode(t *testing.T) {
	// 拉取失败时的回执语义：原因码上得了回执，且计数全为 0（不能把失败洗成 Skip）。
	plan := &acliPluginStagePlan{
		started:  time.Now(),
		fetchErr: newAcliPluginError("plugin_ticket_rejected", "平台返回 401"),
	}
	receipt := (&Bridge{}).syncAcliPluginsFromPlatform(context.Background(), nil, models.InMessage{}, plan, "", "")
	if receipt.OK || receipt.Reason != "plugin_ticket_rejected" {
		t.Fatalf("拉取失败必须带原因码上报：%+v", receipt)
	}
	if receipt.Skipped != 0 || receipt.DesiredCount != 0 {
		t.Fatalf("拉取失败不得凭空计数：%+v", receipt)
	}
	if !strings.Contains(receipt.SummaryText(), "plugin_ticket_rejected") {
		t.Fatalf("汇总文案必须能回答为什么未执行：%s", receipt.SummaryText())
	}
}

func TestAcliPluginSummaryTextNilSafeAndInformative(t *testing.T) {
	var empty *models.AcliPluginStageReceipt
	if empty.SummaryText() != "" {
		t.Fatalf("nil 回执不应产出文案")
	}
	failed := &models.AcliPluginStageReceipt{Reason: "manifest_unreachable"}
	if !strings.Contains(failed.SummaryText(), "manifest_unreachable") {
		t.Fatalf("未执行时必须带原因码：%s", failed.SummaryText())
	}
	done := &models.AcliPluginStageReceipt{Installed: 1, Overwritten: 2, Skipped: 3, Failed: 4, DesiredCount: 10, Nodes: []string{"10.0.0.1", "10.0.0.2"}}
	text := done.SummaryText()
	for _, want := range []string{"新增 1", "覆盖 2", "跳过 3", "失败 4", "期望 10 个插件 / 2 个节点"} {
		if !strings.Contains(text, want) {
			t.Fatalf("回执文案缺少 %q：%s", want, text)
		}
	}
}
